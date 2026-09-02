from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Protocol

from app.controlled_live_execution import NATIVE_ETH_ADDRESS
from app.live_execution_journal import read_live_execution_events
from app.live_trading_config import BASE_USDC_ADDRESS


CAUTIOUS_PROFILE = "cautious_v1"
MEDIUM_HIGH_PROFILE = "medium_high_v1"
STRATEGY_PROFILE_ENV = "TRADING_STRATEGY_PROFILE"
STRATEGY_JOURNAL_PATH = Path("data/strategy_profile_v1.jsonl")
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64

ENTRY_THRESHOLD = 70
WATCH_THRESHOLD = 55
ADDITION_THRESHOLD = 85
MAX_ADDITIONS = 2
ENTRY_COOLDOWN = timedelta(hours=6)
EXIT_COOLDOWN = timedelta(hours=12)
FAILED_COOLDOWN = timedelta(hours=6)
STOP_COOLDOWN = timedelta(hours=24)
ASSET_LOCK = timedelta(days=7)
PORTFOLIO_COOLDOWN = timedelta(hours=24)
MIN_ECONOMIC_EXIT_USDC = Decimal("2")
DUST_REMAINDER_USDC = Decimal("1")


class StrategyProfileError(RuntimeError):
    pass


class SignalLike(Protocol):
    packet_id: str
    observed_at: datetime
    symbol: str
    token_address: str | None
    price_usd: Decimal
    liquidity_usd: Decimal
    daily_volume_usd: Decimal
    change_h6_percent: Decimal
    change_h24_percent: Decimal
    buys_h24: int
    sells_h24: int


class PositionLike(Protocol):
    token_balance: Decimal
    value_usdc: Decimal
    average_entry_price_usdc: Decimal
    cost_basis_verified: bool


@dataclass(frozen=True)
class ExitOutcome:
    recorded_at: datetime
    realized_pl_usdc: Decimal
    exit_reason: str | None


@dataclass(frozen=True)
class AssetCostBasis:
    token_address: str
    confirmed_quantity: Decimal
    remaining_cost_usdc: Decimal
    average_entry_price_usdc: Decimal
    first_entry_at: datetime | None
    last_entry_at: datetime | None
    last_exit_at: datetime | None
    last_failed_at: datetime | None
    confirmed_buy_count: int
    realized_pl_usdc: Decimal
    exits: tuple[ExitOutcome, ...]
    verified: bool = True

    @property
    def additions(self) -> int:
        return max(0, self.confirmed_buy_count - 1)


@dataclass(frozen=True)
class StrategyObservation:
    recorded_at: datetime
    packet_id: str
    token_address: str
    price_usd: Decimal
    volume_usd: Decimal
    entry_score: int


@dataclass(frozen=True)
class StrategyDecision:
    profile: str
    entry_score: int
    components: dict[str, int]
    classification: str
    action: str
    exit_reason: str | None = None
    sell_fraction: Decimal = Decimal("0")
    stop_loss_percent: Decimal | None = None
    trailing_distance_percent: Decimal | None = None


def load_strategy_profile() -> str:
    value = os.getenv(STRATEGY_PROFILE_ENV, CAUTIOUS_PROFILE).strip().lower()
    aliases = {
        "cautious": CAUTIOUS_PROFILE,
        CAUTIOUS_PROFILE: CAUTIOUS_PROFILE,
        "medium_high": MEDIUM_HIGH_PROFILE,
        MEDIUM_HIGH_PROFILE: MEDIUM_HIGH_PROFILE,
    }
    try:
        return aliases[value]
    except KeyError as error:
        raise ValueError(
            f"{STRATEGY_PROFILE_ENV} must be cautious_v1 or medium_high_v1."
        ) from error


def _aware(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise StrategyProfileError(f"{label} is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StrategyProfileError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise StrategyProfileError(f"{label} is invalid.") from error
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise StrategyProfileError(f"{label} is not finite and positive.")
    return parsed


def _contract(value: object, label: str) -> str:
    address = str(value).strip().lower()
    if len(address) != 42 or not address.startswith("0x"):
        raise StrategyProfileError(f"{label} is invalid.")
    return address


def reconstruct_cost_basis(
    *, path: Path
) -> dict[str, AssetCostBasis]:
    """Rebuild weighted-average inventory only from confirmed audit outcomes.

    Reservations, backend failures, and receipt-rejected outcomes never create
    inventory. This intentionally preserves ambiguous reservations as charged
    while excluding them from cost basis until separately reconciled.
    """

    events = read_live_execution_events(path=path)
    reservations: dict[str, dict[str, object]] = {}
    mutable: dict[str, dict[str, object]] = {}
    accounted_intents = {
        str(event.get("intent_id", ""))
        for event in events
        if event.get("event") == "RECONCILIATION_ACCOUNTED"
    }

    def state(address: str) -> dict[str, object]:
        return mutable.setdefault(
            address,
            {
                "quantity": Decimal("0"),
                "cost": Decimal("0"),
                "first_entry": None,
                "last_entry": None,
                "last_exit": None,
                "last_failed": None,
                "buy_count": 0,
                "realized": Decimal("0"),
                "exits": [],
                "verified": True,
            },
        )

    for event in events:
        intent_id = str(event.get("intent_id", ""))
        event_name = event.get("event")
        if event_name == "RESERVED":
            if intent_id in reservations:
                raise StrategyProfileError("Live audit contains duplicate reservations.")
            reservations[intent_id] = event
            continue
        reservation = reservations.get(intent_id)
        if reservation is None:
            raise StrategyProfileError("Live audit outcome has no reservation.")
        if event_name == "RECONCILED_CONFIRMED":
            continue
        from_token = _contract(reservation.get("from_token"), "Swap input token")
        to_token = _contract(reservation.get("to_token"), "Swap output token")
        asset_address = to_token if from_token == BASE_USDC_ADDRESS else from_token
        item = state(asset_address)
        if event_name in {"BACKEND_FAILED", "RECEIPT_REJECTED"}:
            if event_name == "RECEIPT_REJECTED" and intent_id in accounted_intents:
                continue
            recorded_at = _aware(event.get("recorded_at"), "Live outcome timestamp")
            item["last_failed"] = recorded_at
            continue
        if event_name not in {"CONFIRMED", "RECONCILIATION_ACCOUNTED"}:
            raise StrategyProfileError("Live audit contains an unsupported outcome.")
        details = event.get("details")
        if not isinstance(details, dict):
            raise StrategyProfileError("Confirmed receipt details are unavailable.")
        if event_name == "RECONCILIATION_ACCOUNTED":
            reconciliation_sequence = details.get("source_reconciliation_sequence")
            receipt_sequence = details.get("source_receipt_sequence")
            if (
                type(reconciliation_sequence) is not int
                or type(receipt_sequence) is not int
                or not 0 < receipt_sequence < reconciliation_sequence < int(event["sequence"])
            ):
                raise StrategyProfileError(
                    "Reconciled accounting source sequence is invalid."
                )
            reconciliation = events[reconciliation_sequence - 1]
            receipt = events[receipt_sequence - 1]
            receipt_details = receipt.get("details")
            if (
                reconciliation.get("event") != "RECONCILED_CONFIRMED"
                or receipt.get("event") != "RECEIPT_REJECTED"
                or reconciliation.get("intent_id") != intent_id
                or receipt.get("intent_id") != intent_id
                or not isinstance(receipt_details, dict)
                or details.get("transaction_hash")
                != receipt_details.get("transaction_hash")
                or details.get("verification_source") != "public_base_rpc"
                or _contract(details.get("from_token"), "Accounting input token")
                != from_token
                or _contract(details.get("to_token"), "Accounting output token")
                != to_token
                or details.get("from_decimals") != reservation.get("from_decimals")
                or details.get("to_decimals") != reservation.get("to_decimals")
            ):
                raise StrategyProfileError(
                    "Reconciled accounting evidence does not match its receipt."
                )
            recorded_at = _aware(details.get("executed_at"), "Reconciled execution timestamp")
        else:
            recorded_at = _aware(event.get("recorded_at"), "Live outcome timestamp")
        from_amount = _decimal(details.get("from_amount"), "Confirmed input")
        to_amount = _decimal(details.get("to_amount"), "Confirmed output")
        if from_token == BASE_USDC_ADDRESS and to_token != BASE_USDC_ADDRESS:
            quantity = to_amount
            cost = from_amount
            if Decimal(item["quantity"]) == 0:
                item["first_entry"] = recorded_at
                item["buy_count"] = 0
            item["quantity"] = Decimal(item["quantity"]) + quantity
            item["cost"] = Decimal(item["cost"]) + cost
            item["buy_count"] = int(item["buy_count"]) + 1
            item["last_entry"] = recorded_at
        elif to_token == BASE_USDC_ADDRESS and from_token != BASE_USDC_ADDRESS:
            quantity_before = Decimal(item["quantity"])
            cost_before = Decimal(item["cost"])
            tolerance = max(Decimal("0.000000000001"), quantity_before * Decimal("0.001"))
            if quantity_before <= 0 or from_amount > quantity_before + tolerance:
                item["verified"] = False
                item["quantity"] = Decimal("0")
                item["cost"] = Decimal("0")
                item["last_exit"] = recorded_at
                continue
            sold_quantity = min(from_amount, quantity_before)
            sold_cost = cost_before * sold_quantity / quantity_before
            remaining_quantity = quantity_before - sold_quantity
            remaining_cost = cost_before - sold_cost
            realized = to_amount - sold_cost
            item["quantity"] = remaining_quantity
            item["cost"] = remaining_cost
            item["realized"] = Decimal(item["realized"]) + realized
            item["last_exit"] = recorded_at
            item["exits"].append(
                ExitOutcome(
                    recorded_at=recorded_at,
                    realized_pl_usdc=realized,
                    exit_reason=(
                        str(reservation.get("exit_reason"))
                        if reservation.get("exit_reason") is not None
                        else None
                    ),
                )
            )
        else:
            raise StrategyProfileError("Confirmed route is not an asset-USDC spot trade.")

    result: dict[str, AssetCostBasis] = {}
    for address, item in mutable.items():
        quantity = Decimal(item["quantity"])
        cost = Decimal(item["cost"])
        result[address] = AssetCostBasis(
            token_address=address,
            confirmed_quantity=quantity,
            remaining_cost_usdc=cost,
            average_entry_price_usdc=(cost / quantity if quantity > 0 else Decimal("0")),
            first_entry_at=item["first_entry"],
            last_entry_at=item["last_entry"],
            last_exit_at=item["last_exit"],
            last_failed_at=item["last_failed"],
            confirmed_buy_count=int(item["buy_count"]),
            realized_pl_usdc=Decimal(item["realized"]),
            exits=tuple(item["exits"]),
            verified=bool(item["verified"]),
        )
    return result


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _validate_strategy_entries(lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    for sequence, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise StrategyProfileError("Strategy journal contains invalid JSON.") from error
        if (
            not isinstance(entry, dict)
            or entry.get("schema_version") != SCHEMA_VERSION
            or entry.get("sequence") != sequence
            or entry.get("previous_hash") != previous_hash
        ):
            raise StrategyProfileError("Strategy journal sequence or chain is invalid.")
        stored = entry.get("entry_hash")
        unsigned = dict(entry)
        unsigned.pop("entry_hash", None)
        if stored != _hash(unsigned):
            raise StrategyProfileError("Strategy journal hash is invalid.")
        previous_hash = str(stored)
        entries.append(entry)
    return entries


def read_strategy_events(
    *, path: Path = STRATEGY_JOURNAL_PATH
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _validate_strategy_entries(
                [line.strip() for line in handle if line.strip()]
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_strategy_decision(
    *,
    signal: SignalLike,
    decision: StrategyDecision,
    path: Path = STRATEGY_JOURNAL_PATH,
    recorded_at: datetime,
) -> bool:
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("Strategy journal timestamp must include a timezone.")
    address = (signal.token_address or NATIVE_ETH_ADDRESS).lower()
    payload_data: dict[str, object] = {
        "event": "SIGNAL_EVALUATED",
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
        "profile": decision.profile,
        "packet_id": signal.packet_id,
        "asset_symbol": signal.symbol,
        "asset_token_address": address,
        "market_observed_at": signal.observed_at.astimezone(timezone.utc).isoformat(),
        "price_usd": str(signal.price_usd),
        "volume_h24_usd": str(signal.daily_volume_usd),
        "change_h6_percent": str(signal.change_h6_percent),
        "change_h24_percent": str(signal.change_h24_percent),
        "entry_score": decision.entry_score,
        "score_components": decision.components,
        "classification": decision.classification,
        "action": decision.action,
        "exit_reason": decision.exit_reason,
        "sell_fraction": str(decision.sell_fraction),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            entries = _validate_strategy_entries(
                [line.strip() for line in handle if line.strip()]
            )
            for entry in entries:
                if (
                    entry.get("event") == "SIGNAL_EVALUATED"
                    and entry.get("profile") == decision.profile
                    and entry.get("asset_token_address") == address
                    and entry.get("packet_id") == signal.packet_id
                ):
                    comparable = {
                        key: entry.get(key)
                        for key in payload_data
                        if key != "recorded_at"
                    }
                    expected = {
                        key: value
                        for key, value in payload_data.items()
                        if key != "recorded_at"
                    }
                    if comparable != expected:
                        raise StrategyProfileError(
                            "Strategy packet was reused with different evaluation data."
                        )
                    return False
            unsigned = {
                "schema_version": SCHEMA_VERSION,
                "sequence": len(entries) + 1,
                "previous_hash": str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH,
                **payload_data,
            }
            unsigned["entry_hash"] = _hash(unsigned)
            handle.seek(0, 2)
            handle.write(_canonical(unsigned) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def strategy_observations(
    token_address: str,
    *,
    profile: str,
    path: Path = STRATEGY_JOURNAL_PATH,
) -> tuple[StrategyObservation, ...]:
    address = token_address.lower()
    observations = []
    for event in read_strategy_events(path=path):
        if (
            event.get("event") != "SIGNAL_EVALUATED"
            or event.get("profile") != profile
            or event.get("asset_token_address") != address
        ):
            continue
        observations.append(
            StrategyObservation(
                recorded_at=_aware(event.get("recorded_at"), "Observation timestamp"),
                packet_id=str(event.get("packet_id")),
                token_address=address,
                price_usd=_decimal(event.get("price_usd"), "Observation price"),
                volume_usd=_decimal(
                    event.get("volume_h24_usd"), "Observation volume", allow_zero=True
                ),
                entry_score=int(event.get("entry_score")),
            )
        )
    return tuple(observations)


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(high, max(low, value))


def _points(value: Decimal, maximum: int) -> int:
    bounded = _clamp(value, Decimal("0"), Decimal(maximum))
    return int(bounded.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _volatility_percent(
    signal: SignalLike,
    observations: tuple[StrategyObservation, ...],
) -> Decimal:
    recent = observations[-12:]
    returns: list[Decimal] = []
    previous = None
    for item in recent:
        if previous is not None and previous > 0:
            returns.append(abs((item.price_usd - previous) / previous * Decimal("100")))
        previous = item.price_usd
    if returns:
        return sum(returns, Decimal("0")) / Decimal(len(returns))
    return max(
        abs(signal.change_h6_percent),
        abs(signal.change_h24_percent) / Decimal("2"),
    )


def _recent_volume_baseline(
    observations: tuple[StrategyObservation, ...],
    fallback: Decimal,
) -> Decimal:
    volumes = sorted(
        item.volume_usd for item in observations[-12:] if item.volume_usd > 0
    )
    if len(volumes) < 3:
        return fallback
    middle = len(volumes) // 2
    if len(volumes) % 2:
        return volumes[middle]
    return (volumes[middle - 1] + volumes[middle]) / Decimal("2")


def composite_entry_score(
    signal: SignalLike,
    *,
    baseline_volume_usd: Decimal,
    position_value_usdc: Decimal,
    portfolio_value_usdc: Decimal,
    additions: int,
    notional_usdc: Decimal = Decimal("20"),
) -> tuple[int, dict[str, int]]:
    """Return the deterministic medium-high score and its seven components."""

    h6 = _points((signal.change_h6_percent + Decimal("6")) / Decimal("12") * 20, 20)
    h24 = _points((signal.change_h24_percent + Decimal("12")) / Decimal("24") * 20, 20)
    transactions = signal.buys_h24 + signal.sells_h24
    imbalance = (
        Decimal(signal.buys_h24 - signal.sells_h24) / Decimal(transactions)
        if transactions > 0
        else Decimal("-1")
    )
    imbalance_points = _points((imbalance + Decimal("0.2")) / Decimal("0.8") * 15, 15)
    volume_ratio = (
        signal.daily_volume_usd / baseline_volume_usd
        if baseline_volume_usd > 0
        else Decimal("0")
    )
    volume_points = _points((volume_ratio - Decimal("0.5")) * 15, 15)
    liquidity_ratio = signal.liquidity_usd / Decimal("100000")
    liquidity_headroom = Decimal("5") + _clamp(
        (liquidity_ratio - Decimal("1")) / Decimal("4"),
        Decimal("0"),
        Decimal("1"),
    ) * Decimal("10")
    impact_bps = (
        notional_usdc / signal.liquidity_usd * Decimal("10000")
        if signal.liquidity_usd > 0
        else Decimal("10000")
    )
    impact_quality = _clamp(
        (Decimal("100") - impact_bps) / Decimal("100"),
        Decimal("0"),
        Decimal("1"),
    ) * Decimal("15")
    liquidity_points = _points(min(liquidity_headroom, impact_quality), 15)
    if signal.change_h6_percent > 0 and signal.change_h24_percent > 0:
        consistency = 10
    elif signal.change_h6_percent > 0 or signal.change_h24_percent > 0:
        weak = min(signal.change_h6_percent, signal.change_h24_percent)
        consistency = _points(Decimal("6") + weak / Decimal("2"), 10)
    else:
        consistency = 0
    exposure_percent = (
        position_value_usdc / portfolio_value_usdc * Decimal("100")
        if portfolio_value_usdc > 0
        else Decimal("100")
    )
    exposure_points = _points(
        Decimal("5") - exposure_percent / Decimal("5") - Decimal(additions),
        5,
    )
    components = {
        "momentum_h6": h6,
        "momentum_h24": h24,
        "transaction_imbalance": imbalance_points,
        "relative_volume": volume_points,
        "liquidity_impact": liquidity_points,
        "trend_consistency": consistency,
        "exposure_history": exposure_points,
    }
    return sum(components.values()), components


def evaluate_cautious(
    signal: SignalLike,
    *,
    position: PositionLike | None,
    baseline_volume_usd: Decimal,
    portfolio_value_usdc: Decimal,
) -> StrategyDecision:
    """Represent the existing rules for parallel shadow comparison only."""

    score, components = composite_entry_score(
        signal,
        baseline_volume_usd=baseline_volume_usd,
        position_value_usdc=(
            position.value_usdc if position is not None else Decimal("0")
        ),
        portfolio_value_usdc=portfolio_value_usdc,
        additions=0,
    )
    buy = (
        signal.change_h6_percent > 0
        and signal.change_h24_percent > 0
        and signal.buys_h24 > signal.sells_h24
    )
    exit_reason = None
    if position is not None:
        stop = bool(
            position.cost_basis_verified
            and position.average_entry_price_usdc > 0
            and signal.price_usd
            <= position.average_entry_price_usdc * Decimal("0.92")
        )
        profit = bool(
            position.cost_basis_verified
            and position.average_entry_price_usdc > 0
            and signal.price_usd
            >= position.average_entry_price_usdc * Decimal("1.15")
        )
        reversal = signal.change_h6_percent < 0 and signal.change_h24_percent < 0
        if stop:
            exit_reason = "legacy_stop_loss"
        elif profit:
            exit_reason = "legacy_take_profit"
        elif reversal:
            exit_reason = "legacy_dual_momentum_reversal"
    action = "sell" if exit_reason else "buy" if buy and position is None else "hold"
    return StrategyDecision(
        profile=CAUTIOUS_PROFILE,
        entry_score=score,
        components=components,
        classification=("eligible" if action != "hold" else "rejected"),
        action=action,
        exit_reason=exit_reason,
        sell_fraction=Decimal("1") if action == "sell" else Decimal("0"),
    )


def _cooldown_reason(
    basis: AssetCostBasis | None,
    *,
    now: datetime,
) -> str | None:
    if basis is None:
        return None
    stop_times = [
        item.recorded_at
        for item in basis.exits
        if item.exit_reason == "hard_stop_loss" and now - item.recorded_at <= ASSET_LOCK
    ]
    if len(stop_times) >= 2 and now - max(stop_times) < ASSET_LOCK:
        return "asset_locked_after_repeated_stop_losses"
    candidates = (
        (basis.last_entry_at, ENTRY_COOLDOWN, "entry_cooldown"),
        (basis.last_exit_at, EXIT_COOLDOWN, "exit_cooldown"),
        (basis.last_failed_at, FAILED_COOLDOWN, "failed_transaction_cooldown"),
    )
    for timestamp, duration, reason in candidates:
        if timestamp is not None and now - timestamp < duration:
            return reason
    if stop_times and now - max(stop_times) < STOP_COOLDOWN:
        return "stop_loss_cooldown"
    return None


def portfolio_cooldown_reason(
    bases: dict[str, AssetCostBasis], *, now: datetime
) -> str | None:
    outcomes = sorted(
        (item for basis in bases.values() for item in basis.exits),
        key=lambda item: item.recorded_at,
    )
    consecutive = 0
    for item in outcomes:
        consecutive = consecutive + 1 if item.realized_pl_usdc < 0 else 0
    if consecutive >= 3 and outcomes and now - outcomes[-1].recorded_at < PORTFOLIO_COOLDOWN:
        return "consecutive_loss_guard"
    recent_losses = [
        item for item in outcomes
        if item.realized_pl_usdc < 0 and now - item.recorded_at <= timedelta(hours=24)
    ]
    if len(recent_losses) >= 3 and now - recent_losses[-1].recorded_at < PORTFOLIO_COOLDOWN:
        return "portfolio_loss_cooldown"
    return None


def evaluate_medium_high(
    signal: SignalLike,
    *,
    position: PositionLike | None,
    basis: AssetCostBasis | None,
    all_bases: dict[str, AssetCostBasis],
    baseline_volume_usd: Decimal,
    portfolio_value_usdc: Decimal,
    observations: tuple[StrategyObservation, ...],
    now: datetime,
) -> StrategyDecision:
    position_value = position.value_usdc if position is not None else Decimal("0")
    additions = basis.additions if basis is not None else 0
    baseline_volume_usd = _recent_volume_baseline(
        observations,
        baseline_volume_usd,
    )
    score, components = composite_entry_score(
        signal,
        baseline_volume_usd=baseline_volume_usd,
        position_value_usdc=position_value,
        portfolio_value_usdc=portfolio_value_usdc,
        additions=additions,
    )
    classification = (
        "strong_entry" if score >= 85 else
        "entry" if score >= ENTRY_THRESHOLD else
        "watch" if score >= WATCH_THRESHOLD else
        "rejected"
    )
    volatility = _volatility_percent(signal, observations)
    stop_percent = _clamp(
        Decimal("8") + volatility * Decimal("0.25"),
        Decimal("8"),
        Decimal("12"),
    )
    liquidity_penalty = (
        Decimal("2")
        if signal.liquidity_usd < Decimal("250000")
        else Decimal("0")
    )
    trailing = _clamp(
        Decimal("6") + volatility * Decimal("0.15") + liquidity_penalty,
        Decimal("6"),
        Decimal("9"),
    )

    if position is not None:
        if (
            basis is None
            or not basis.verified
            or not position.cost_basis_verified
            or basis.confirmed_quantity <= 0
        ):
            return StrategyDecision(
                MEDIUM_HIGH_PROFILE, score, components, classification,
                "hold", "cost_basis_unverified", stop_loss_percent=stop_percent,
                trailing_distance_percent=trailing,
            )
        if (
            basis.last_failed_at is not None
            and now - basis.last_failed_at < FAILED_COOLDOWN
        ):
            return StrategyDecision(
                MEDIUM_HIGH_PROFILE, score, components, classification,
                "hold", "failed_transaction_cooldown",
                stop_loss_percent=stop_percent,
                trailing_distance_percent=trailing,
            )
        average = basis.average_entry_price_usdc
        profit_percent = (signal.price_usd / average - Decimal("1")) * Decimal("100")
        position_observations = tuple(
            item
            for item in observations
            if basis.first_entry_at is None or item.recorded_at >= basis.first_entry_at
        )
        prices = [item.price_usd for item in position_observations]
        highest = max(prices + [signal.price_usd, average])
        trailing_drawdown = (highest - signal.price_usd) / highest * Decimal("100")
        prior_low = bool(
            position_observations and position_observations[-1].entry_score < 35
        )
        age = now - (basis.first_entry_at or now)
        peak_profit = (highest / average - Decimal("1")) * Decimal("100")
        partial_taken = any(
            item.exit_reason == "partial_profit_15"
            and (
                basis.first_entry_at is None
                or item.recorded_at >= basis.first_entry_at
            )
            for item in basis.exits
        )
        if profit_percent <= -stop_percent:
            reason, fraction = "hard_stop_loss", Decimal("1")
        elif profit_percent >= Decimal("10") and trailing_drawdown >= trailing:
            reason, fraction = "trailing_profit_stop", Decimal("1")
        elif profit_percent >= Decimal("25"):
            reason, fraction = "final_profit_target_25", Decimal("1")
        elif profit_percent >= Decimal("15") and not partial_taken:
            reason, fraction = "partial_profit_15", Decimal("0.5")
        elif signal.change_h6_percent < 0 and signal.change_h24_percent < 0:
            reason, fraction = "dual_momentum_reversal", Decimal("1")
        elif score < 35 and prior_low:
            reason, fraction = "two_cycle_score_breakdown", Decimal("1")
        elif age >= timedelta(hours=72) and peak_profit < Decimal("3"):
            reason, fraction = "stagnant_72h_exit", Decimal("1")
        elif age >= timedelta(hours=48) and peak_profit < Decimal("3"):
            reason, fraction = "stagnant_48h_reduction", Decimal("0.5")
        else:
            reason, fraction = None, Decimal("0")
        if reason is not None:
            if position.value_usdc < MIN_ECONOMIC_EXIT_USDC:
                return StrategyDecision(
                    MEDIUM_HIGH_PROFILE, score, components, classification,
                    "hold", "dust_exit_suppressed", stop_loss_percent=stop_percent,
                    trailing_distance_percent=trailing,
                )
            if (
                fraction < 1
                and position.value_usdc * (Decimal("1") - fraction) < DUST_REMAINDER_USDC
            ):
                fraction = Decimal("1")
            return StrategyDecision(
                MEDIUM_HIGH_PROFILE, score, components, classification,
                "sell", reason, fraction, stop_percent, trailing,
            )

    cooldown = _cooldown_reason(basis, now=now) or portfolio_cooldown_reason(
        all_bases, now=now
    )
    if cooldown is not None:
        return StrategyDecision(
            MEDIUM_HIGH_PROFILE, score, components, classification, "hold", cooldown,
            stop_loss_percent=stop_percent, trailing_distance_percent=trailing,
        )
    if position is None and score >= ENTRY_THRESHOLD and (
        signal.change_h6_percent > 0 or signal.change_h24_percent > 0
    ):
        return StrategyDecision(
            MEDIUM_HIGH_PROFILE, score, components, classification, "buy",
            stop_loss_percent=stop_percent, trailing_distance_percent=trailing,
        )
    if position is not None and score >= ADDITION_THRESHOLD:
        profitable = (
            basis is not None
            and position.cost_basis_verified
            and signal.price_usd > basis.average_entry_price_usdc
        )
        if profitable and additions < MAX_ADDITIONS:
            return StrategyDecision(
                MEDIUM_HIGH_PROFILE, score, components, classification, "add",
                stop_loss_percent=stop_percent, trailing_distance_percent=trailing,
            )
    return StrategyDecision(
        MEDIUM_HIGH_PROFILE, score, components, classification, "hold",
        stop_loss_percent=stop_percent, trailing_distance_percent=trailing,
    )


def strategy_metrics(
    *,
    strategy_journal_path: Path,
    live_audit_path: Path,
    risk_journal_path: Path | None = None,
) -> dict[str, object]:
    signals = read_strategy_events(path=strategy_journal_path)
    audit = read_live_execution_events(path=live_audit_path)
    confirmed = [
        item
        for item in audit
        if item.get("event") in {"CONFIRMED", "RECONCILIATION_ACCOUNTED"}
    ]
    bases = reconstruct_cost_basis(path=live_audit_path)
    receipt_spreads = []
    gas_costs = []
    turnover = Decimal("0")
    reservations = {
        str(item.get("intent_id")): item
        for item in audit
        if item.get("event") == "RESERVED"
    }
    profile_metrics: dict[str, dict[str, object]] = {}
    for profile in (CAUTIOUS_PROFILE, MEDIUM_HIGH_PROFILE):
        profile_signals = [item for item in signals if item.get("profile") == profile]
        profile_confirmed = [
            item
            for item in confirmed
            if reservations.get(str(item.get("intent_id")), {}).get(
                "strategy_profile", CAUTIOUS_PROFILE
            )
            == profile
        ]
        profile_metrics[profile] = {
            "eligible_signals": sum(
                1
                for item in profile_signals
                if item.get("action") in {"buy", "add", "sell"}
            ),
            "rejected_signals": sum(
                1 for item in profile_signals if item.get("action") == "hold"
            ),
            "entries": sum(
                1
                for item in profile_confirmed
                if reservations.get(str(item.get("intent_id")), {}).get("exit_reason")
                is None
            ),
            "exits": sum(
                1
                for item in profile_confirmed
                if reservations.get(str(item.get("intent_id")), {}).get("exit_reason")
                is not None
            ),
            "turnover_usdc": str(
                sum(
                    (
                        Decimal(
                            str(
                                reservations[str(item.get("intent_id"))][
                                    "notional_usdc"
                                ]
                            )
                        )
                        for item in profile_confirmed
                        if str(item.get("intent_id")) in reservations
                    ),
                    Decimal("0"),
                )
            ),
        }
    for item in confirmed:
        reservation = reservations.get(str(item.get("intent_id")))
        if reservation is not None:
            turnover += _decimal(reservation.get("notional_usdc"), "Turnover notional")
        details = item.get("details")
        if not isinstance(details, dict):
            continue
        to_amount = _decimal(details.get("to_amount"), "Receipt output")
        minimum = _decimal(details.get("min_to_amount"), "Receipt minimum")
        receipt_spreads.append((to_amount - minimum) / to_amount * Decimal("10000"))
        if details.get("gas_cost_eth") is not None:
            gas_costs.append(_decimal(details["gas_cost_eth"], "Gas cost", allow_zero=True))
    risk_entries = []
    if risk_journal_path is not None:
        from app.live_portfolio_risk import read_live_portfolio_risk

        risk_entries = read_live_portfolio_risk(path=risk_journal_path)
    return {
        "eligible_signals": sum(
            1 for item in signals if item.get("action") in {"buy", "add", "sell"}
        ),
        "rejected_signals": sum(
            1 for item in signals if item.get("classification") == "rejected"
        ),
        "watch_signals": sum(
            1 for item in signals if item.get("classification") == "watch"
        ),
        "entries": sum(
            1 for item in confirmed
            if reservations.get(str(item.get("intent_id")), {}).get("entry_score") is not None
            and reservations.get(str(item.get("intent_id")), {}).get("exit_reason") is None
        ),
        "exits": sum(
            1 for item in confirmed
            if reservations.get(str(item.get("intent_id")), {}).get("exit_reason") is not None
        ),
        "realized_pl_usdc": str(sum((item.realized_pl_usdc for basis in bases.values() for item in basis.exits), Decimal("0"))),
        "receipt_spread_bps_average": (
            str(sum(receipt_spreads, Decimal("0")) / Decimal(len(receipt_spreads)))
            if receipt_spreads else None
        ),
        "gas_cost_eth": str(sum(gas_costs, Decimal("0"))) if gas_costs else None,
        "turnover_usdc": str(turnover),
        "latest_drawdown_percent": (
            str(risk_entries[-1]["drawdown_percent"]) if risk_entries else None
        ),
        "maximum_observed_drawdown_percent": (
            str(max(Decimal(str(item["drawdown_percent"])) for item in risk_entries))
            if risk_entries else None
        ),
        "profiles": profile_metrics,
    }
