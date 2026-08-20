from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import app.paper_cycle_ledger as paper_ledger
from app.paper_cycle_ledger import LedgerConflictError
from app.paper_portfolio import PaperPortfolio
from app.paper_trader import STARTING_BALANCE


RISK_STATE_PATH = Path("data/paper_risk_state_v2.json")
RISK_LOCK_PATH = Path("data/paper_risk_state_v2.lock")
STATE_VERSION = 2
MAX_DAILY_LOSS_PERCENT = Decimal("5")
MAX_DRAWDOWN_PERCENT = Decimal("20")
PERCENT_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class RiskAccountingState:
    high_water_mark: Decimal
    daily_start_value: Decimal
    daily_date: date
    last_portfolio_value: Decimal
    updated_at: datetime
    ledger_sequence: int
    ledger_head: str


@dataclass(frozen=True)
class RiskAccountingDecision:
    ready: bool
    reason: str
    current_value: Decimal | None = None
    high_water_mark: Decimal | None = None
    daily_start_value: Decimal | None = None
    drawdown_percent: Decimal | None = None
    daily_loss_percent: Decimal | None = None
    daily_date: date | None = None
    drawdown_halt: bool = False
    daily_loss_halt: bool = False


class RiskAccountingTransaction:
    """Public transaction interface; append authority exists only in its lexical subtype."""

    def commit_cycle(
        self,
        payload: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], dict[str, object], bool]:
        return self._commit_cycle(payload)

    def _commit_cycle(
        self,
        payload: dict[str, object] | None,
    ) -> tuple[dict[str, Any], dict[str, object], bool]:
        raise ValueError("Risk accounting unavailable; cycle commit is blocked.")


def _validate_payload_state(
    payload: dict[str, object],
    state: RiskAccountingState,
) -> None:
    try:
        recorded_at = datetime.fromisoformat(str(payload["recorded_at"]))
        accounting_date = date.fromisoformat(str(payload["accounting_date"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Cycle risk metadata is invalid.") from error
    expected = {
        "portfolio_value": state.last_portfolio_value,
        "high_water_mark": state.high_water_mark,
        "daily_start_value": state.daily_start_value,
    }
    for key, value in expected.items():
        if _decimal(payload, key, positive=key != "portfolio_value") != value:
            raise ValueError(f"Cycle {key} does not match the coordinated risk state.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("Cycle recorded_at must include a timezone.")
    if recorded_at.astimezone(timezone.utc) != state.updated_at:
        raise ValueError("Cycle recorded_at does not match the risk update time.")
    if accounting_date != state.daily_date:
        raise ValueError("Cycle accounting date does not match the risk state.")


def portfolio_value(portfolio: PaperPortfolio, reference_price: Decimal) -> Decimal:
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("Reference price must be finite and positive.")
    if not portfolio.usdc_balance.is_finite() or portfolio.usdc_balance < 0:
        raise ValueError("USDC balance must be finite and nonnegative.")
    if not portfolio.eth_balance.is_finite() or portfolio.eth_balance < 0:
        raise ValueError("ETH balance must be finite and nonnegative.")
    value = portfolio.usdc_balance + portfolio.eth_balance * reference_price
    if not value.is_finite() or value < 0:
        raise ValueError("Portfolio value must be finite and nonnegative.")
    return value


def _decimal(data: dict[str, object], key: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(str(data[key]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Risk state field {key} is invalid.") from error
    if not value.is_finite() or value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"Risk state field {key} must be finite and {qualifier}.")
    return value


def _parse_state(data: object) -> RiskAccountingState:
    if not isinstance(data, dict):
        raise ValueError("Risk state must be a JSON object.")
    if data.get("version") != STATE_VERSION:
        raise ValueError("Risk state version is unsupported.")
    try:
        daily_date = date.fromisoformat(str(data["daily_date"]))
        updated_at = datetime.fromisoformat(str(data["updated_at"]))
        ledger_sequence = int(data["ledger_sequence"])
        ledger_head = str(data["ledger_head"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Risk state metadata is invalid.") from error
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError("Risk state update time must include a timezone.")
    if ledger_sequence <= 0 or len(ledger_head) != 64:
        raise ValueError("Risk state ledger binding is invalid.")
    high_water_mark = _decimal(data, "high_water_mark", positive=True)
    daily_start_value = _decimal(data, "daily_start_value", positive=True)
    last_portfolio_value = _decimal(data, "last_portfolio_value", positive=False)
    if high_water_mark < daily_start_value:
        raise ValueError("Risk state daily start exceeds its high-water mark.")
    if high_water_mark < last_portfolio_value:
        raise ValueError("Risk state portfolio value exceeds its high-water mark.")
    if updated_at.astimezone(timezone.utc).date() != daily_date:
        raise ValueError("Risk state date does not match its update time.")
    return RiskAccountingState(
        high_water_mark=high_water_mark,
        daily_start_value=daily_start_value,
        daily_date=daily_date,
        last_portfolio_value=last_portfolio_value,
        updated_at=updated_at.astimezone(timezone.utc),
        ledger_sequence=ledger_sequence,
        ledger_head=ledger_head,
    )


def _load_state() -> RiskAccountingState | None:
    if not RISK_STATE_PATH.exists():
        return None
    with RISK_STATE_PATH.open("r", encoding="utf-8") as state_file:
        return _parse_state(json.load(state_file))


def _save_state(state: RiskAccountingState) -> None:
    RISK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "ledger_sequence": state.ledger_sequence,
        "ledger_head": state.ledger_head,
        "high_water_mark": str(state.high_water_mark),
        "daily_start_value": str(state.daily_start_value),
        "daily_date": state.daily_date.isoformat(),
        "last_portfolio_value": str(state.last_portfolio_value),
        "updated_at": state.updated_at.isoformat(),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RISK_STATE_PATH.parent,
            prefix=f".{RISK_STATE_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(payload, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(RISK_STATE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _state_from_ledger(records: list[dict[str, Any]]) -> RiskAccountingState | None:
    state: RiskAccountingState | None = None
    for record in records:
        try:
            updated_at = datetime.fromisoformat(str(record["recorded_at"]))
            if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                raise ValueError("Ledger risk update time must include a timezone.")
            updated_at = updated_at.astimezone(timezone.utc)
            current_value = _decimal(record, "portfolio_value", positive=False)
            expected = _next_state(current_value, updated_at, state)
            daily_date = date.fromisoformat(
                str(record.get("accounting_date") or updated_at.date().isoformat())
            )
            candidate = RiskAccountingState(
                high_water_mark=_decimal(record, "high_water_mark", positive=True),
                daily_start_value=_decimal(record, "daily_start_value", positive=True),
                daily_date=daily_date,
                last_portfolio_value=current_value,
                updated_at=updated_at,
                ledger_sequence=int(record["sequence"]),
                ledger_head=str(record["entry_hash"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"Ledger risk fields at sequence {record.get('sequence')} are invalid."
            ) from error
        if (
            candidate.high_water_mark != expected.high_water_mark
            or candidate.daily_start_value != expected.daily_start_value
            or candidate.daily_date != expected.daily_date
            or candidate.last_portfolio_value != expected.last_portfolio_value
            or candidate.updated_at != expected.updated_at
        ):
            raise ValueError("Ledger risk transition is not deterministic.")
        state = candidate
    return state


def _reconcile_cache(records: list[dict[str, Any]]) -> RiskAccountingState | None:
    authoritative = _state_from_ledger(records)
    cache_exists = RISK_STATE_PATH.exists()
    try:
        cached = _load_state()
    except (OSError, json.JSONDecodeError, ValueError):
        cached = None
    if authoritative is None:
        if cache_exists:
            raise ValueError("Risk cache exists without an authoritative ledger.")
        return None
    if cached is not None:
        if cached.ledger_sequence > len(records):
            raise ValueError("Risk cache is ahead of the authoritative ledger.")
        bound_record = records[cached.ledger_sequence - 1]
        if cached.ledger_head != str(bound_record["entry_hash"]):
            raise ValueError("Risk cache binding is inconsistent with the ledger.")
    if cached != authoritative:
        _save_state(authoritative)
    return authoritative


def _loss_percent(start: Decimal, current: Decimal) -> Decimal:
    if current >= start:
        return Decimal("0")
    return ((start - current) / start * Decimal("100")).quantize(PERCENT_QUANTUM)


def _prepare_risk_update(
    portfolio: PaperPortfolio,
    reference_price: Decimal,
    current_time: datetime,
    state: RiskAccountingState | None,
) -> tuple[RiskAccountingDecision, RiskAccountingState]:
    current_value = portfolio_value(portfolio, reference_price)
    proposed = _next_state(current_value, current_time, state)
    high_water_mark = proposed.high_water_mark
    daily_start_value = proposed.daily_start_value
    drawdown_percent = _loss_percent(high_water_mark, current_value)
    daily_loss_percent = _loss_percent(daily_start_value, current_value)
    drawdown_halt = drawdown_percent >= MAX_DRAWDOWN_PERCENT
    daily_loss_halt = daily_loss_percent >= MAX_DAILY_LOSS_PERCENT
    if drawdown_halt:
        reason = "High-water-mark drawdown limit reached."
    elif daily_loss_halt:
        reason = "Daily loss limit reached; new positions are blocked."
    else:
        reason = "Portfolio risk accounting passed."
    return RiskAccountingDecision(
        ready=True,
        reason=reason,
        current_value=current_value,
        high_water_mark=high_water_mark,
        daily_start_value=daily_start_value,
        drawdown_percent=drawdown_percent,
        daily_loss_percent=daily_loss_percent,
        daily_date=current_time.date(),
        drawdown_halt=drawdown_halt,
        daily_loss_halt=daily_loss_halt,
    ), proposed


def _next_state(
    current_value: Decimal,
    current_time: datetime,
    state: RiskAccountingState | None,
) -> RiskAccountingState:
    if state is None:
        high_water_mark = max(STARTING_BALANCE, current_value)
        daily_start_value = current_value
    else:
        if current_time.date() < state.daily_date:
            raise ValueError("Risk-accounting clock moved before its saved day.")
        if current_time < state.updated_at:
            raise ValueError("Risk-accounting clock moved before its last update.")
        high_water_mark = max(state.high_water_mark, current_value)
        daily_start_value = state.daily_start_value
        if current_time.date() > state.daily_date:
            daily_start_value = state.last_portfolio_value
    return RiskAccountingState(
        high_water_mark=high_water_mark,
        daily_start_value=daily_start_value,
        daily_date=current_time.date(),
        last_portfolio_value=current_value,
        updated_at=current_time,
        ledger_sequence=state.ledger_sequence if state else 0,
        ledger_head=state.ledger_head if state else "0" * 64,
    )


def _derive_payload_state(
    payload: dict[str, object],
    records: list[dict[str, Any]],
) -> RiskAccountingState:
    try:
        recorded_at = datetime.fromisoformat(str(payload["recorded_at"]))
        before = payload["portfolio_before"]
        if not isinstance(before, dict):
            raise ValueError("Cycle portfolio_before must be an object.")
        portfolio = PaperPortfolio(
            _decimal(before, "usdc_balance", positive=False),
            _decimal(before, "eth_balance", positive=False),
        )
        reference_price = _decimal(payload, "reference_price", positive=True)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Cycle portfolio risk evidence is invalid.") from error
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("Cycle recorded_at must include a timezone.")
    current_time = recorded_at.astimezone(timezone.utc)
    previous = _state_from_ledger(records)
    current_value = portfolio_value(portfolio, reference_price)
    return _next_state(current_value, current_time, previous)


_STABLE_SIGNAL_FIELDS = (
    "cycle_id",
    "signal_id",
    "strategy_id",
    "strategy_version",
    "acceptance_policy_version",
    "signal",
    "reference_price",
    "maximum_risk",
    "market_data_observed_at",
    "market_data_received_at",
    "kill_switch_state",
    "safety_allowed",
    "safety_reason",
    "research_ready",
    "research_reason",
    "research_packet_ids",
    "research_qualities",
)


def _find_stable_duplicate(
    records: list[dict[str, Any]],
    evidence: dict[str, object] | None,
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    cycle_id = str(evidence.get("cycle_id", ""))
    signal_id = str(evidence.get("signal_id", ""))
    if len(cycle_id) != 64 or cycle_id != signal_id:
        raise ValueError("Cycle and signal IDs must be the same stable SHA-256 value.")
    duplicate = next(
        (record for record in records if str(record.get("cycle_id")) == cycle_id),
        None,
    )
    if duplicate is None:
        return None
    if any(duplicate.get(field) != evidence.get(field) for field in _STABLE_SIGNAL_FIELDS):
        raise LedgerConflictError(
            "Duplicate signal ID was reused with different immutable signal evidence."
        )
    return duplicate


def _decision_from_record(record: dict[str, Any]) -> RiskAccountingDecision:
    try:
        drawdown_percent = _decimal(record, "drawdown_percent", positive=False)
        daily_loss_percent = _decimal(record, "daily_loss_percent", positive=False)
        return RiskAccountingDecision(
            ready=bool(record["accounting_ready"]),
            reason=str(record["accounting_reason"]),
            current_value=_decimal(record, "portfolio_value", positive=False),
            high_water_mark=_decimal(record, "high_water_mark", positive=True),
            daily_start_value=_decimal(record, "daily_start_value", positive=True),
            drawdown_percent=drawdown_percent,
            daily_loss_percent=daily_loss_percent,
            daily_date=date.fromisoformat(str(record["accounting_date"])),
            drawdown_halt=drawdown_percent >= MAX_DRAWDOWN_PERCENT,
            daily_loss_halt=daily_loss_percent >= MAX_DAILY_LOSS_PERCENT,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Recorded duplicate risk evidence is invalid.") from error


class _UnavailableRiskTransaction(RiskAccountingTransaction):
    duplicate = False
    duplicate_entry = None

    def __init__(self, decision: RiskAccountingDecision) -> None:
        self.decision = decision

@contextmanager
def portfolio_risk_transaction(
    portfolio: PaperPortfolio,
    reference_price: Decimal,
    *,
    now: datetime | None = None,
    signal_evidence: dict[str, object] | None = None,
) -> Iterator[Any]:
    """Coordinate risk reconciliation and commit using ledger-then-risk locks.

    This is the sole lock order: acquire the paper-ledger lock first, validate
    it, then acquire the risk-cache lock. Both remain held through append and
    cache persistence, so reconciliation cannot deadlock with a cycle commit.
    """
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        yield _UnavailableRiskTransaction(
            RiskAccountingDecision(False, "Risk-accounting time must include a timezone.")
        )
        return
    paper_ledger.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    paper_ledger.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    RISK_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    ledger_lock = None
    risk_lock = None
    try:
        ledger_lock = paper_ledger.LOCK_PATH.open("a+", encoding="utf-8")
        fcntl.flock(ledger_lock.fileno(), fcntl.LOCK_EX)
        records = paper_ledger._load_unlocked()
        duplicate_entry = _find_stable_duplicate(records, signal_evidence)
        risk_lock = RISK_LOCK_PATH.open("a+", encoding="utf-8")
        fcntl.flock(risk_lock.fileno(), fcntl.LOCK_EX)
        state = _reconcile_cache(records)
        if duplicate_entry is None:
            decision, proposed = _prepare_risk_update(
                portfolio,
                reference_price,
                current_time.astimezone(timezone.utc),
                state,
            )
        else:
            decision = _decision_from_record(duplicate_entry)
            proposed = None
    except LedgerConflictError:
        if risk_lock is not None:
            risk_lock.close()
        if ledger_lock is not None:
            ledger_lock.close()
        raise
    except (OSError, json.JSONDecodeError, ValueError) as error:
        if risk_lock is not None:
            risk_lock.close()
        if ledger_lock is not None:
            ledger_lock.close()
        yield _UnavailableRiskTransaction(
            RiskAccountingDecision(False, f"Risk accounting unavailable: {error}")
        )
        return

    active = True

    class CoordinatedRiskTransaction(RiskAccountingTransaction):
        """Lexically scoped commit authority valid only under both locks."""

        def __init__(self) -> None:
            self.duplicate = duplicate_entry is not None
            self.duplicate_entry = duplicate_entry
            self.decision = decision

        def _commit_cycle(
            self,
            payload: dict[str, object] | None = None,
        ) -> tuple[dict[str, Any], dict[str, object], bool]:
            nonlocal active
            if not active:
                raise RuntimeError("Risk transaction is no longer active.")
            if duplicate_entry is not None:
                return (
                    duplicate_entry,
                    paper_ledger._acceptance_summary(
                        records, now=datetime.now(timezone.utc)
                    ),
                    True,
                )
            if payload is None or proposed is None:
                raise ValueError("A complete cycle payload is required.")
            cycle_id = str(payload.get("cycle_id", ""))
            signal_id = str(payload.get("signal_id", ""))
            if len(cycle_id) != 64 or cycle_id != signal_id:
                raise ValueError(
                    "Cycle and signal IDs must be the same stable SHA-256 value."
                )
            supplied_fingerprint = paper_ledger._cycle_fingerprint(payload)
            for record in records:
                if str(record.get("cycle_id")) == cycle_id:
                    if paper_ledger._cycle_fingerprint(record) != supplied_fingerprint:
                        raise LedgerConflictError(
                            "Duplicate signal ID was reused with different cycle evidence."
                        )
                    return (
                        record,
                        paper_ledger._acceptance_summary(
                            records, now=datetime.now(timezone.utc)
                        ),
                        True,
                    )
            authoritative = _derive_payload_state(payload, records)
            _validate_payload_state(payload, authoritative)
            if authoritative != proposed:
                raise ValueError(
                    "Cycle risk transition changed after evaluation; append is blocked."
                )
            expected_before = (
                (paper_ledger.STARTING_USDC, paper_ledger.STARTING_ETH)
                if not records
                else paper_ledger._portfolio(
                    records[-1]["portfolio_after"], "portfolio_after"
                )
            )
            supplied_before = paper_ledger._portfolio(
                payload.get("portfolio_before"), "portfolio_before"
            )
            paper_ledger._portfolio(payload.get("portfolio_after"), "portfolio_after")
            if supplied_before != expected_before:
                raise LedgerConflictError(
                    "Paper cycle was computed from a stale portfolio; no entry was appended."
                )
            entry: dict[str, Any] = {
                "schema_version": paper_ledger.SCHEMA_VERSION,
                "sequence": len(records) + 1,
                "previous_hash": records[-1]["entry_hash"] if records else "0" * 64,
                **payload,
                "cycle_fingerprint": supplied_fingerprint,
            }
            entry["acceptance"] = paper_ledger._acceptance_summary(
                records + [entry],
                now=datetime.fromisoformat(str(entry["recorded_at"])),
            )
            entry["entry_hash"] = paper_ledger._entry_hash(entry)
            encoded = json.dumps(entry, sort_keys=True) + "\n"
            with paper_ledger.LEDGER_PATH.open("a", encoding="utf-8") as ledger_handle:
                ledger_handle.write(encoded)
                ledger_handle.flush()
                os.fsync(ledger_handle.fileno())
            records.append(entry)
            committed = RiskAccountingState(
                high_water_mark=proposed.high_water_mark,
                daily_start_value=proposed.daily_start_value,
                daily_date=proposed.daily_date,
                last_portfolio_value=proposed.last_portfolio_value,
                updated_at=proposed.updated_at,
                ledger_sequence=int(entry["sequence"]),
                ledger_head=str(entry["entry_hash"]),
            )
            # The append is durable before this cache write. Restart
            # reconciliation repairs a crash in that narrow window.
            _save_state(committed)
            return (
                entry,
                paper_ledger._acceptance_summary(records, now=datetime.now(timezone.utc)),
                False,
            )

    try:
        yield CoordinatedRiskTransaction()
    finally:
        active = False
        risk_lock.close()
        ledger_lock.close()


def evaluate_portfolio_risk(
    portfolio: PaperPortfolio,
    reference_price: Decimal,
    *,
    now: datetime | None = None,
) -> RiskAccountingDecision:
    """Evaluate against reconciled committed state without committing a transition."""
    with portfolio_risk_transaction(portfolio, reference_price, now=now) as transaction:
        return transaction.decision
