from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError

from app.asset_lifecycle import (
    AssetLifecycle,
    HistoricalGovernedContract,
    LifecycleAsset,
)
from app.agent_commerce_research import (
    AgentCommerceResearchGate,
    build_research_gate,
    research_public_status,
)
from app.base_asset_universe import (
    AssetUniverseError,
    GovernedAssetUniverse,
    load_governed_asset_universe,
)
from app.base_asset_universe_refresh import refresh_governed_asset_universe
from app.controlled_live_execution import (
    CDP_NETWORK_ID,
    NATIVE_ETH_ADDRESS,
    CdpAgentKitBackend,
    ControlledLiveResult,
    SwapBackend,
)
from app.live_portfolio_risk import record_live_portfolio_value
from app.live_execution_journal import read_live_execution_events
from app.live_trading_config import (
    BASE_USDC_ADDRESS,
    LiveTradingConfig,
    load_live_trading_config,
)
from app.portfolio_trading import (
    PortfolioPosition,
    ResearchSignal,
    VerifiedPortfolio,
    execute_research_portfolio_signal,
    research_signal_from_packet,
    valuation_signal_from_packet,
)
from app.research_feed import get_research_payload
from app.strategy_profile import (
    CAUTIOUS_PROFILE,
    MEDIUM_HIGH_PROFILE,
    STRATEGY_JOURNAL_PATH,
    StrategyDecision,
    StrategyProfileError,
    append_strategy_decision,
    evaluate_cautious,
    evaluate_medium_high,
    load_strategy_profile,
    read_strategy_events,
    reconstruct_cost_basis,
    strategy_metrics,
    strategy_observations,
)
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    KILL_SWITCH_ARMED,
    MAX_TRADING_CAPITAL_USDC,
    ExecutorConfig,
    load_executor_config,
)


CYCLE_NO_FUNDS = "NO_FUNDS_READY"
CYCLE_POLICY_BLOCKED = "POLICY_BLOCKED"
CYCLE_NO_SIGNAL = "NO_ELIGIBLE_SIGNAL"
CYCLE_VALUATION_BLOCKED = "VALUATION_BLOCKED"
CYCLE_QUARANTINED = "QUARANTINED_HOLDINGS"
LIVE_WORKER_INTERVAL_SECONDS = 60
VALUATION_OUTAGE_COOLDOWN_SECONDS = 120
RESEARCH_ENVELOPE_FIELDS = {
    "service",
    "schema_version",
    "mode",
    "execution",
    "generated_at",
    "packets",
}
RESEARCH_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"


@dataclass(frozen=True)
class OnchainTokenBalance:
    token_address: str
    amount: Decimal
    decimals: int


@dataclass(frozen=True)
class LiveCycleResult:
    status: str
    wallet_address: str
    network_id: str
    portfolio_value_usdc: Decimal
    reason: str
    transaction_hash: str | None = None
    trading_readiness: str = "blocked"
    held_required: int = 0
    held_covered: int = 0
    quarantined_count: int = 0


class LiveRuntime(SwapBackend, Protocol):
    wallet_address: str
    network_id: str

    def list_token_balances(self) -> tuple[OnchainTokenBalance, ...]:
        ...


def _research_signals(
    payload: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
    valuation_contracts: frozenset[str] = frozenset(),
) -> tuple[ResearchSignal, ...]:
    if not isinstance(payload, dict) or set(payload) != RESEARCH_ENVELOPE_FIELDS:
        raise ValueError("Research envelope fields are invalid.")
    if (
        payload.get("service") != "lumen-base-research-agent"
        or payload.get("schema_version") != 2
        or payload.get("mode") != "observation_only"
        or payload.get("execution") != "disabled"
    ):
        raise ValueError("Research envelope authority is invalid.")
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except ValueError as error:
        raise ValueError("Research envelope timestamp is invalid.") from error
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("Research envelope timestamp must include a timezone.")
    generated_at = generated_at.astimezone(timezone.utc)
    age = (now - generated_at).total_seconds()
    if age > 120 or age < -30:
        raise ValueError("Research envelope is stale or from the future.")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("Research packets are unavailable.")
    accepted: dict[tuple[str, str | None], ResearchSignal] = {}
    for packet in packets:
        contract = (
            str(packet.get("contract_address", "")).lower()
            if isinstance(packet, dict)
            else ""
        )
        try:
            if contract in valuation_contracts:
                signal = valuation_signal_from_packet(packet, contract, now=now)
                if contract == RESEARCH_WETH_ADDRESS:
                    signal = replace(signal, token_address=None)
            else:
                signal = research_signal_from_packet(packet, universe, now=now)
        except ValueError:
            continue
        identity = (signal.symbol, signal.token_address)
        previous = accepted.get(identity)
        if previous is None or signal.observed_at > previous.observed_at:
            accepted[identity] = signal
        elif (
            signal.observed_at == previous.observed_at
            and signal.packet_id != previous.packet_id
        ):
            raise ValueError("Research packets are ambiguous for a governed asset.")
    return tuple(accepted.values())


def _verified_portfolio(
    balances: tuple[OnchainTokenBalance, ...],
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
    *,
    wallet_address: str,
    native_gas_reserve_eth: Decimal,
    now: datetime,
    lifecycle_assets: tuple[LifecycleAsset, ...] | None = None,
    cost_bases: dict[str, object] | None = None,
) -> VerifiedPortfolio:
    if not native_gas_reserve_eth.is_finite() or native_gas_reserve_eth < 0:
        raise ValueError("Native gas reserve must be finite and non-negative.")
    signal_by_contract = {
        (signal.token_address or NATIVE_ETH_ADDRESS).lower(): signal
        for signal in signals
    }
    lifecycle_by_contract = {
        (asset.token_address or NATIVE_ETH_ADDRESS).lower(): asset
        for asset in lifecycle_assets or ()
    }
    seen: set[str] = set()
    usdc = Decimal("0")
    positions: list[PortfolioPosition] = []
    for balance in balances:
        address = balance.token_address.strip().lower()
        if address in seen:
            raise ValueError("CDP returned a duplicate token balance.")
        seen.add(address)
        if (
            not balance.amount.is_finite()
            or balance.amount < 0
            or type(balance.decimals) is not int
            or not 0 <= balance.decimals <= 36
        ):
            raise ValueError("CDP returned an invalid token balance.")
        spendable_amount = balance.amount
        if address == NATIVE_ETH_ADDRESS:
            spendable_amount = max(
                Decimal("0"),
                balance.amount - native_gas_reserve_eth,
            )
        if spendable_amount == 0:
            continue
        if address == BASE_USDC_ADDRESS:
            if balance.decimals != 6:
                raise ValueError("Official Base USDC decimals do not match.")
            usdc = balance.amount
            continue
        matched = lifecycle_by_contract.get(address)
        if matched is None and lifecycle_assets is None:
            for asset in universe.assets:
                expected = (asset.token_address or NATIVE_ETH_ADDRESS).lower()
                if address == expected:
                    matched = asset
                    break
        if matched is None:
            continue
        if balance.decimals != matched.decimals:
            raise ValueError("Governed token decimals do not match the CDP balance.")
        signal = signal_by_contract.get(address)
        if signal is None:
            raise ValueError("A held governed asset has no fresh valuation signal.")
        value = spendable_amount * signal.price_usd
        basis = cost_bases.get(address) if cost_bases is not None else None
        basis_quantity = (
            basis.confirmed_quantity if basis is not None else Decimal("0")
        )
        quantum = Decimal(1).scaleb(-balance.decimals)
        basis_tolerance = max(
            quantum * Decimal("10"),
            basis_quantity * Decimal("0.001"),
        )
        basis_verified = bool(
            basis is not None
            and basis.verified
            and basis_quantity > 0
            and abs(spendable_amount - basis_quantity) <= basis_tolerance
        )
        positions.append(
            PortfolioPosition(
                symbol=signal.symbol,
                token_address=signal.token_address,
                token_balance=spendable_amount,
                value_usdc=value,
                average_entry_price_usdc=(
                    basis.average_entry_price_usdc
                    if basis_verified
                    else Decimal("0")
                ),
                cost_basis_verified=basis_verified,
            )
        )
    return VerifiedPortfolio(
        observed_at=now,
        treasury_address=wallet_address,
        total_value_usdc=usdc
        + sum((item.value_usdc for item in positions), Decimal("0")),
        usdc_balance=usdc,
        positions=tuple(positions),
    )


def _historical_governance(path: Path) -> tuple[HistoricalGovernedContract, ...]:
    records: dict[str, HistoricalGovernedContract] = {}
    for event in read_live_execution_events(path=path):
        if event.get("event") != "RESERVED":
            continue
        recorded_at = datetime.fromisoformat(str(event.get("recorded_at")))
        for token_field, decimals_field in (
            ("from_token", "from_decimals"),
            ("to_token", "to_decimals"),
        ):
            address = str(event.get(token_field, "")).lower()
            if address == BASE_USDC_ADDRESS:
                continue
            decimals = event.get(decimals_field)
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError("Live journal contains an invalid governed contract.")
            if type(decimals) is not int or not 0 <= decimals <= 36:
                raise ValueError("Live journal contains invalid governed decimals.")
            previous = records.get(address)
            if previous is not None and previous.decimals != decimals:
                raise ValueError("Live journal governed decimals are contradictory.")
            records[address] = HistoricalGovernedContract(
                token_address=address,
                decimals=decimals,
                governed_at=recorded_at,
            )
    return tuple(records.values())


def _ordered_signals(
    signals: tuple[ResearchSignal, ...],
    portfolio: VerifiedPortfolio,
    universe: GovernedAssetUniverse,
    *,
    strategy_profile: str = CAUTIOUS_PROFILE,
    cost_bases: dict[str, object] | None = None,
    strategy_journal_path: Path = STRATEGY_JOURNAL_PATH,
    now: datetime | None = None,
) -> tuple[ResearchSignal, ...]:
    held = {(item.symbol, item.token_address) for item in portfolio.positions}
    rank = {(item.symbol, item.token_address): item.rank for item in universe.assets}

    def priority(signal: ResearchSignal) -> tuple[int, Decimal, int]:
        identity = (signal.symbol, signal.token_address)
        if strategy_profile == MEDIUM_HIGH_PROFILE:
            position = next(
                (
                    item
                    for item in portfolio.positions
                    if (item.symbol, item.token_address) == identity
                ),
                None,
            )
            address = (signal.token_address or NATIVE_ETH_ADDRESS).lower()
            decision = evaluate_medium_high(
                signal,
                position=position,
                basis=(cost_bases or {}).get(address),
                all_bases=cost_bases or {},
                baseline_volume_usd=universe.require(
                    signal.symbol,
                    signal.token_address,
                ).daily_volume_usd,
                portfolio_value_usdc=portfolio.total_value_usdc,
                observations=strategy_observations(
                    address,
                    profile=MEDIUM_HIGH_PROFILE,
                    path=strategy_journal_path,
                ),
                now=now or portfolio.observed_at,
            )
            category = (
                0
                if decision.action == "sell"
                else 1
                if decision.action in {"buy", "add"}
                else 2
            )
            return (
                category,
                Decimal(-decision.entry_score),
                rank.get(identity, 10_000),
            )
        exit_signal = (
            identity in held
            and signal.change_h6_percent < 0
            and signal.change_h24_percent < 0
        )
        buy_signal = (
            signal.change_h6_percent > 0
            and signal.change_h24_percent > 0
            and signal.buys_h24 > signal.sells_h24
        )
        category = 0 if exit_signal else 1 if buy_signal else 2
        momentum = -(signal.change_h6_percent + signal.change_h24_percent)
        return category, momentum, rank.get(identity, 10_000)

    return tuple(sorted(signals, key=priority))


def _execution_universe(
    current: GovernedAssetUniverse,
    lifecycle_assets: tuple[LifecycleAsset, ...],
) -> GovernedAssetUniverse:
    assets = list(current.assets)
    known = {(item.symbol, item.token_address) for item in assets}
    retained = sorted(
        (
            item.asset
            for item in lifecycle_assets
            if item.asset is not None
            and (item.asset.symbol, item.asset.token_address) not in known
        ),
        key=lambda item: ((item.token_address or NATIVE_ETH_ADDRESS), item.symbol),
    )
    assets.extend(retained)
    if not retained:
        return current
    retained_ids = ":".join(
        f"{item.symbol}:{item.token_address or NATIVE_ETH_ADDRESS}"
        for item in retained
    )
    digest = hashlib.sha256(
        f"{current.snapshot_sha256}:retained:{retained_ids}".encode()
    ).hexdigest()
    return GovernedAssetUniverse(
        observed_at=current.observed_at,
        source=f"{current.source}+retained-exit-only",
        snapshot_sha256=digest,
        assets=tuple(assets),
    )


def _execution_eligible_signals(
    signals: tuple[ResearchSignal, ...],
    universe: GovernedAssetUniverse,
) -> tuple[ResearchSignal, ...]:
    """Remove candidates that the downstream asset-liquidity policy must reject."""

    accepted = []
    for signal in signals:
        asset = universe.require(signal.symbol, signal.token_address)
        if signal.liquidity_usd >= asset.liquidity_usd / Decimal("2"):
            accepted.append(signal)
    return tuple(accepted)


def run_live_cycle(
    *,
    runtime: LiveRuntime,
    research_payload: object | Callable[..., object],
    universe: GovernedAssetUniverse | Callable[[], GovernedAssetUniverse],
    authorized_capital_usdc: Decimal,
    decision_journal_path: Path,
    live_audit_path: Path,
    risk_journal_path: Path,
    native_gas_reserve_eth: Decimal = Decimal("0"),
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    agent_commerce_research_gate: AgentCommerceResearchGate | None = None,
    lifecycle_registry_path: Path | None = None,
    strategy_profile: str = CAUTIOUS_PROFILE,
    strategy_journal_path: Path = STRATEGY_JOURNAL_PATH,
    parallel_shadow: bool = False,
) -> LiveCycleResult:
    """Verify live inputs and make at most one governed execution attempt."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("Live cycle time must include a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    wallet = runtime.wallet_address.strip().lower()
    if wallet != AUTHORIZED_TREASURY_ADDRESS or runtime.network_id != CDP_NETWORK_ID:
        raise ValueError("CDP runtime wallet or network is not authorized.")
    if (
        not authorized_capital_usdc.is_finite()
        or authorized_capital_usdc <= 0
        or authorized_capital_usdc > MAX_TRADING_CAPITAL_USDC
    ):
        raise ValueError("Authorized capital must be positive and at most $500.")
    balances = runtime.list_token_balances()
    if not balances:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
        )
    resolved_universe = universe() if callable(universe) else universe
    lifecycle = AssetLifecycle(
        lifecycle_registry_path or live_audit_path.parent / "asset_lifecycle.json"
    )
    lifecycle_assessment = lifecycle.evaluate(
        resolved_universe,
        balances,
        now=current_time,
        historical_governance=_historical_governance(live_audit_path),
    )
    execution_universe = _execution_universe(
        resolved_universe,
        lifecycle_assessment.held_governed,
    )
    try:
        cost_bases = reconstruct_cost_basis(path=live_audit_path)
    except (OSError, StrategyProfileError, ValueError) as error:
        raise ValueError(f"Persistent cost basis is unavailable: {error}") from error
    held_research_contracts = frozenset(
        (
            RESEARCH_WETH_ADDRESS
            if item.token_address is None
            or item.token_address.lower() == NATIVE_ETH_ADDRESS
            else item.token_address.lower()
        )
        for item in lifecycle_assessment.held_governed
    )
    try:
        resolved_research = (
            research_payload(lifecycle_assessment.required_research_contracts)
            if callable(research_payload)
            else research_payload
        )
        valuation_signals = _research_signals(
            resolved_research,
            resolved_universe,
            now=current_time,
            valuation_contracts=held_research_contracts,
        )
        trade_signals = _research_signals(
            resolved_research,
            resolved_universe,
            now=current_time,
        )
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        return LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            wallet,
            runtime.network_id,
            Decimal("0"),
            f"Research evidence blocked: {_safe_error_message(error)}",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=0,
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    lifecycle_identity = {
        (item.token_address or NATIVE_ETH_ADDRESS).lower(): item
        for item in lifecycle_assessment.held_governed
    }
    valuation_signals = tuple(
        replace(
            signal,
            symbol=lifecycle_identity[
                (signal.token_address or NATIVE_ETH_ADDRESS).lower()
            ].symbol,
            token_address=lifecycle_identity[
                (signal.token_address or NATIVE_ETH_ADDRESS).lower()
            ].token_address,
        )
        if (signal.token_address or NATIVE_ETH_ADDRESS).lower() in lifecycle_identity
        else signal
        for signal in valuation_signals
    )
    signal_contracts = {
        (signal.token_address or NATIVE_ETH_ADDRESS).lower()
        for signal in valuation_signals
    }
    missing_held = [
        item
        for item in lifecycle_assessment.held_governed
        if (item.token_address or NATIVE_ETH_ADDRESS).lower() not in signal_contracts
    ]
    if missing_held:
        return LiveCycleResult(
            CYCLE_VALUATION_BLOCKED,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Fresh exact-contract valuation is missing for a governed holding.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed) - len(missing_held),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    portfolio = _verified_portfolio(
        balances,
        valuation_signals,
        resolved_universe,
        wallet_address=wallet,
        native_gas_reserve_eth=native_gas_reserve_eth,
        now=current_time,
        lifecycle_assets=lifecycle_assessment.held_governed,
        cost_bases=cost_bases,
    )
    if portfolio.total_value_usdc == 0:
        return LiveCycleResult(
            CYCLE_NO_FUNDS,
            wallet,
            runtime.network_id,
            Decimal("0"),
            "Exact CDP wallet and Base network verified with no governed funds.",
            trading_readiness="blocked",
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    risk = record_live_portfolio_value(
        portfolio.total_value_usdc,
        authorized_capital_usdc=authorized_capital_usdc,
        path=risk_journal_path,
        now=current_time,
    )
    candidate_contracts = frozenset(lifecycle_assessment.candidate_contracts)
    candidate_signals = tuple(
        signal
        for signal in trade_signals
        if (
            RESEARCH_WETH_ADDRESS
            if signal.token_address is None
            else signal.token_address.lower()
        )
        in candidate_contracts
    )
    held_identities = {
        (item.symbol, item.token_address) for item in portfolio.positions
    }
    raw_medium_candidates = {
        (signal.symbol, signal.token_address): signal
        for signal in candidate_signals
    }
    for signal in valuation_signals:
        if (signal.symbol, signal.token_address) in held_identities:
            raw_medium_candidates[(signal.symbol, signal.token_address)] = signal
    medium_candidates = {
        identity: signal
        for identity, signal in raw_medium_candidates.items()
        if execution_universe.contains(signal.symbol, signal.token_address)
    }
    unroutable_medium = tuple(
        signal
        for identity, signal in raw_medium_candidates.items()
        if identity not in medium_candidates
    )
    try:
        evaluated_medium_packets = {
            (
                str(event.get("asset_token_address")),
                str(event.get("packet_id")),
            )
            for event in read_strategy_events(path=strategy_journal_path)
            if event.get("event") == "SIGNAL_EVALUATED"
            and event.get("profile") == MEDIUM_HIGH_PROFILE
        }
    except (OSError, StrategyProfileError, ValueError) as error:
        raise ValueError(f"Strategy journal unavailable: {error}") from error

    def medium_packet_is_new(candidate: ResearchSignal) -> bool:
        address = (candidate.token_address or NATIVE_ETH_ADDRESS).lower()
        return (address, candidate.packet_id) not in evaluated_medium_packets

    if parallel_shadow or strategy_profile == MEDIUM_HIGH_PROFILE:
        try:
            for candidate in unroutable_medium:
                append_strategy_decision(
                    signal=candidate,
                    decision=StrategyDecision(
                        profile=MEDIUM_HIGH_PROFILE,
                        entry_score=0,
                        components={
                            "momentum_h6": 0,
                            "momentum_h24": 0,
                            "transaction_imbalance": 0,
                            "relative_volume": 0,
                            "liquidity_impact": 0,
                            "trend_consistency": 0,
                            "exposure_history": 0,
                        },
                        classification="rejected",
                        action="hold",
                        exit_reason="governance_metadata_unavailable",
                    ),
                    path=strategy_journal_path,
                    recorded_at=current_time,
                )
        except (OSError, StrategyProfileError, ValueError) as error:
            raise ValueError(f"Strategy journal update failed: {error}") from error
    if parallel_shadow:
        positions = {
            (item.symbol, item.token_address): item for item in portfolio.positions
        }
        try:
            for candidate in candidate_signals:
                append_strategy_decision(
                    signal=candidate,
                    decision=evaluate_cautious(
                        candidate,
                        position=positions.get(
                            (candidate.symbol, candidate.token_address)
                        ),
                        baseline_volume_usd=execution_universe.require(
                            candidate.symbol,
                            candidate.token_address,
                        ).daily_volume_usd,
                        portfolio_value_usdc=portfolio.total_value_usdc,
                    ),
                    path=strategy_journal_path,
                    recorded_at=current_time,
                )
            for candidate in medium_candidates.values():
                if not medium_packet_is_new(candidate):
                    continue
                address = (candidate.token_address or NATIVE_ETH_ADDRESS).lower()
                append_strategy_decision(
                    signal=candidate,
                    decision=evaluate_medium_high(
                        candidate,
                        position=positions.get(
                            (candidate.symbol, candidate.token_address)
                        ),
                        basis=cost_bases.get(address),
                        all_bases=cost_bases,
                        baseline_volume_usd=execution_universe.require(
                            candidate.symbol,
                            candidate.token_address,
                        ).daily_volume_usd,
                        portfolio_value_usdc=portfolio.total_value_usdc,
                        observations=strategy_observations(
                            address,
                            profile=MEDIUM_HIGH_PROFILE,
                            path=strategy_journal_path,
                        ),
                        now=current_time,
                    ),
                    path=strategy_journal_path,
                    recorded_at=current_time,
                )
        except (OSError, StrategyProfileError, ValueError) as error:
            raise ValueError(f"Parallel strategy shadow failed: {error}") from error
    if strategy_profile == MEDIUM_HIGH_PROFILE:
        execution_signals = tuple(
            candidate
            for candidate in medium_candidates.values()
            if medium_packet_is_new(candidate)
        )
        positions = {
            (item.symbol, item.token_address): item for item in portfolio.positions
        }
        medium_decisions: dict[tuple[str, str | None], StrategyDecision] = {}
        try:
            for candidate in execution_signals:
                address = (candidate.token_address or NATIVE_ETH_ADDRESS).lower()
                decision = evaluate_medium_high(
                    candidate,
                    position=positions.get(
                        (candidate.symbol, candidate.token_address)
                    ),
                    basis=cost_bases.get(address),
                    all_bases=cost_bases,
                    baseline_volume_usd=execution_universe.require(
                        candidate.symbol,
                        candidate.token_address,
                    ).daily_volume_usd,
                    portfolio_value_usdc=portfolio.total_value_usdc,
                    observations=strategy_observations(
                        address,
                        profile=MEDIUM_HIGH_PROFILE,
                        path=strategy_journal_path,
                    ),
                    now=current_time,
                )
                append_strategy_decision(
                    signal=candidate,
                    decision=decision,
                    path=strategy_journal_path,
                    recorded_at=current_time,
                )
                medium_decisions[(candidate.symbol, candidate.token_address)] = decision
        except (OSError, StrategyProfileError, ValueError) as error:
            raise ValueError(f"Strategy journal update failed: {error}") from error
    else:
        execution_signals = _execution_eligible_signals(
            candidate_signals,
            resolved_universe,
        )
    if not execution_signals:
        return LiveCycleResult(
            CYCLE_NO_SIGNAL,
            wallet,
            runtime.network_id,
            portfolio.total_value_usdc,
            "No fresh governed research signal passes execution liquidity policy.",
            trading_readiness=(
                "ready"
                if live_config.enabled
                and executor_config.mode == EXECUTOR_MODE_CONTROLLED_LIVE
                and executor_config.kill_switch_state == KILL_SWITCH_ARMED
                else "blocked"
            ),
            held_required=len(lifecycle_assessment.held_governed),
            held_covered=len(lifecycle_assessment.held_governed),
            quarantined_count=len(lifecycle_assessment.quarantined),
        )
    selected = _ordered_signals(
        execution_signals,
        portfolio,
        execution_universe,
        strategy_profile=strategy_profile,
        cost_bases=cost_bases,
        strategy_journal_path=strategy_journal_path,
        now=current_time,
    )[0]
    result: ControlledLiveResult = execute_research_portfolio_signal(
        selected,
        portfolio,
        risk,
        execution_universe,
        runtime,
        decision_journal_path=decision_journal_path,
        live_audit_path=live_audit_path,
        now=current_time,
        live_config=live_config,
        executor_config=executor_config,
        agent_commerce_research_gate=agent_commerce_research_gate,
        strategy_profile=strategy_profile,
        cost_bases=cost_bases,
        strategy_journal_path=strategy_journal_path,
        precomputed_strategy_decision=(
            medium_decisions.get((selected.symbol, selected.token_address))
            if strategy_profile == MEDIUM_HIGH_PROFILE
            else None
        ),
    )
    status = result.status
    if status == "POLICY_REJECTED":
        status = CYCLE_POLICY_BLOCKED
    return LiveCycleResult(
        status,
        wallet,
        runtime.network_id,
        portfolio.total_value_usdc,
        " ".join(result.reasons),
        result.transaction_hash,
        trading_readiness=("ready" if status not in {CYCLE_POLICY_BLOCKED} else "blocked"),
        held_required=len(lifecycle_assessment.held_governed),
        held_covered=len(lifecycle_assessment.held_governed),
        quarantined_count=len(lifecycle_assessment.quarantined),
    )


class CdpLiveRuntime:
    def __init__(self) -> None:
        self._backend = CdpAgentKitBackend()
        self.wallet_address = self._backend.wallet_address
        self.network_id = self._backend.network_id

    def list_token_balances(self) -> tuple[OnchainTokenBalance, ...]:
        return tuple(
            OnchainTokenBalance(address, amount, decimals)
            for address, amount, decimals in self._backend.list_token_balances()
        )

    def submit_swap(self, request):
        return self._backend.submit_swap(request)


STATE: dict[str, object] = {
    "mode": "controlled_live_worker",
    "status": "starting",
    "operational_status": "starting",
    "trading_readiness": "blocked",
    "cycle_status": "starting",
    "last_cycle_at": None,
    "last_error": None,
    "last_error_message": None,
    "last_block_reason": None,
    "correlation_id": None,
    "held_required": 0,
    "held_covered": 0,
    "quarantined_count": 0,
    "safety_gates": {
        "live_trading_enabled": False,
        "executor_mode": "shadow_only",
        "kill_switch": "halted",
        "agent_commerce_research": "disabled",
        "strategy_profile": CAUTIOUS_PROFILE,
        "parallel_strategy_shadow": False,
    },
    "agent_commerce_research": research_public_status("disabled"),
    "strategy_metrics": {},
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "service": "crypto-trading-agent",
                "schema_version": 2,
                "mode": STATE["mode"],
                "status": STATE["status"],
                "operational_status": STATE["operational_status"],
                "trading_readiness": STATE["trading_readiness"],
                "cycle_status": STATE["cycle_status"],
                "last_cycle_at": STATE["last_cycle_at"],
                "last_error": STATE["last_error"],
                "last_error_message": STATE["last_error_message"],
                "last_block_reason": STATE["last_block_reason"],
                "correlation_id": STATE["correlation_id"],
                "held_required": STATE["held_required"],
                "held_covered": STATE["held_covered"],
                "quarantined_count": STATE["quarantined_count"],
                "safety_gates": STATE["safety_gates"],
                "agent_commerce_research": STATE["agent_commerce_research"],
                "strategy_metrics": STATE["strategy_metrics"],
            }
        ).encode()
        self.send_response(
            503 if STATE["operational_status"] == "failed" else 200
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _authorized_capital() -> Decimal:
    try:
        value = Decimal(os.getenv("LIVE_AUTHORIZED_CAPITAL_USDC", "500"))
    except InvalidOperation as error:
        raise ValueError("LIVE_AUTHORIZED_CAPITAL_USDC is invalid.") from error
    if not value.is_finite() or value <= 0 or value > MAX_TRADING_CAPITAL_USDC:
        raise ValueError(
            "LIVE_AUTHORIZED_CAPITAL_USDC must be positive and at most 500."
        )
    return value


def _worker_enabled() -> bool:
    value = os.getenv("LIVE_WORKER_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("LIVE_WORKER_ENABLED must be true or false.")
    return value == "true"


def _parallel_shadow_enabled() -> bool:
    value = os.getenv("TRADING_PARALLEL_SHADOW_ENABLED", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("TRADING_PARALLEL_SHADOW_ENABLED must be true or false.")
    return value == "true"


def _native_gas_reserve() -> Decimal:
    try:
        value = Decimal(os.getenv("LIVE_NATIVE_GAS_RESERVE_ETH", "0"))
    except InvalidOperation as error:
        raise ValueError("LIVE_NATIVE_GAS_RESERVE_ETH is invalid.") from error
    if not value.is_finite() or value < 0:
        raise ValueError("LIVE_NATIVE_GAS_RESERVE_ETH must be finite and non-negative.")
    return value


def load_or_refresh_universe(path: Path, *, now: datetime) -> GovernedAssetUniverse:
    """Keep execution halted until a current exact-25 snapshot is available."""

    try:
        return load_governed_asset_universe(path, now=now)
    except AssetUniverseError:
        STATE.update(cycle_status="refreshing_universe", trading_readiness="blocked")
        refresh_governed_asset_universe(path)
        return load_governed_asset_universe(path, now=datetime.now(timezone.utc))


def _safety_gates(
    live_config: LiveTradingConfig,
    executor_config: ExecutorConfig,
    research_gate: AgentCommerceResearchGate,
    strategy_profile: str,
    parallel_shadow: bool,
) -> dict[str, object]:
    return {
        "live_trading_enabled": live_config.enabled,
        "executor_mode": executor_config.mode,
        "kill_switch": executor_config.kill_switch_state,
        "agent_commerce_research": research_gate.mode,
        "strategy_profile": strategy_profile,
        "parallel_strategy_shadow": parallel_shadow,
    }


def _safe_error_message(error: BaseException) -> str:
    message = str(error).strip() or "No diagnostic message supplied."
    message = re.sub(r"https?://[^\s]+", "[redacted-url]", message)
    message = re.sub(r"0x[0-9a-fA-F]{40,64}", "[redacted-hex]", message)
    return message[:240]


def _record_cycle_result(
    result: LiveCycleResult,
    *,
    cycle_time: datetime,
    correlation_id: str,
    safety_gates: dict[str, object],
    metrics: dict[str, object] | None = None,
) -> None:
    STATE.update(
        status="operational",
        operational_status="operational",
        trading_readiness=result.trading_readiness,
        cycle_status=result.status.lower(),
        last_cycle_at=cycle_time.isoformat(),
        last_error=None,
        last_error_message=None,
        last_block_reason=(
            _safe_error_message(ValueError(result.reason))
            if result.trading_readiness == "blocked"
            else None
        ),
        correlation_id=correlation_id,
        held_required=result.held_required,
        held_covered=result.held_covered,
        quarantined_count=result.quarantined_count,
        safety_gates=safety_gates,
        strategy_metrics=metrics or {},
    )
    print(
        json.dumps(
            {
                "event": "live_cycle_completed",
                "correlation_id": correlation_id,
                "cycle_status": result.status,
                "trading_readiness": result.trading_readiness,
                "held_required": result.held_required,
                "held_covered": result.held_covered,
                "quarantined_count": result.quarantined_count,
                "transaction_submitted": result.transaction_hash is not None,
                "block_reason": (
                    _safe_error_message(ValueError(result.reason))
                    if result.trading_readiness == "blocked"
                    else None
                ),
            }
        ),
        flush=True,
    )


def _record_cycle_failure(
    error: BaseException,
    *,
    cycle_time: datetime,
    correlation_id: str,
) -> None:
    message = _safe_error_message(error)
    STATE.update(
        status="failed",
        operational_status="failed",
        trading_readiness="blocked",
        cycle_status="integrity_failure",
        last_cycle_at=cycle_time.isoformat(),
        last_error=type(error).__name__,
        last_error_message=message,
        last_block_reason=message,
        correlation_id=correlation_id,
    )
    print(
        json.dumps(
            {
                "event": "live_cycle_failed",
                "correlation_id": correlation_id,
                "stage": "integrity_or_runtime",
                "error_type": type(error).__name__,
                "message": message,
                "trading_readiness": "blocked",
            }
        ),
        flush=True,
    )


def _cycle_sleep_seconds(interval: int, result: LiveCycleResult) -> int:
    if result.status == CYCLE_VALUATION_BLOCKED:
        return max(interval, VALUATION_OUTAGE_COOLDOWN_SECONDS)
    return interval


def main() -> None:
    server = HTTPServer(
        ("0.0.0.0", int(os.getenv("PORT", "8080"))),  # nosec B104
        HealthHandler,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not _worker_enabled():
        STATE.update(
            status="operational",
            operational_status="operational",
            trading_readiness="blocked",
            cycle_status="disabled",
        )
        while True:
            time.sleep(3600)
    runtime = CdpLiveRuntime()
    research_gate = build_research_gate(
        wallet_address=runtime.wallet_address,
        journal_path=Path(
            os.getenv(
                "LUMEN_AGENT_COMMERCE_RESEARCH_JOURNAL_PATH",
                "data/agent_commerce_research_v1.jsonl",
            )
        ),
    )
    STATE["agent_commerce_research"] = research_public_status(research_gate.mode)
    interval = int(
        os.getenv(
            "LIVE_WORKER_INTERVAL_SECONDS",
            str(LIVE_WORKER_INTERVAL_SECONDS),
        )
    )
    if not 30 <= interval <= 3600:
        raise ValueError("LIVE_WORKER_INTERVAL_SECONDS must be between 30 and 3600.")
    while True:
        cycle_time = datetime.now(timezone.utc)
        correlation_id = uuid.uuid4().hex[:16]
        cycle_delay = interval
        try:
            live_config = load_live_trading_config()
            executor_config = load_executor_config()
            strategy_profile = load_strategy_profile()
            parallel_shadow = _parallel_shadow_enabled()
            safety_gates = _safety_gates(
                live_config,
                executor_config,
                research_gate,
                strategy_profile,
                parallel_shadow,
            )
            result = run_live_cycle(
                runtime=runtime,
                research_payload=get_research_payload,
                universe=lambda: load_or_refresh_universe(
                    Path(
                        os.getenv(
                            "LIVE_ASSET_UNIVERSE_PATH",
                            "data/base_top25_universe.json",
                        )
                    ),
                    now=cycle_time,
                ),
                authorized_capital_usdc=_authorized_capital(),
                decision_journal_path=Path("data/execution_decisions.jsonl"),
                live_audit_path=Path("data/live_execution_audit.jsonl"),
                risk_journal_path=Path("data/live_portfolio_risk.jsonl"),
                native_gas_reserve_eth=_native_gas_reserve(),
                now=cycle_time,
                live_config=live_config,
                executor_config=executor_config,
                agent_commerce_research_gate=research_gate,
                strategy_profile=strategy_profile,
                strategy_journal_path=Path(
                    os.getenv(
                        "TRADING_STRATEGY_JOURNAL_PATH",
                        str(STRATEGY_JOURNAL_PATH),
                    )
                ),
                parallel_shadow=parallel_shadow,
            )
            _record_cycle_result(
                result,
                cycle_time=cycle_time,
                correlation_id=correlation_id,
                safety_gates=safety_gates,
                metrics=strategy_metrics(
                    strategy_journal_path=Path(
                        os.getenv(
                            "TRADING_STRATEGY_JOURNAL_PATH",
                            str(STRATEGY_JOURNAL_PATH),
                        )
                    ),
                    live_audit_path=Path("data/live_execution_audit.jsonl"),
                    risk_journal_path=Path("data/live_portfolio_risk.jsonl"),
                ),
            )
            cycle_delay = _cycle_sleep_seconds(interval, result)
        except Exception as error:
            _record_cycle_failure(
                error,
                cycle_time=cycle_time,
                correlation_id=correlation_id,
            )
        time.sleep(cycle_delay)


if __name__ == "__main__":
    main()
