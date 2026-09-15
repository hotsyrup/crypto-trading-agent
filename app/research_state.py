"""Normalize versioned observation-only research for deterministic consumers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.live_trading_config import BASE_USDC_ADDRESS
from app.portfolio_trading import (
    ALLOWED_RESEARCH_WARNINGS,
    ResearchSignal,
    WETH_ADDRESS,
    research_signal_from_packet,
)
from app.research_contract_v3 import (
    AcceptedAssetFailure,
    AcceptedMarketSnapshot,
    validate_research_bundle,
)


V2_ENVELOPE_FIELDS = {
    "service",
    "schema_version",
    "mode",
    "execution",
    "generated_at",
    "packets",
}
V2_VALUATION_WARNINGS = ALLOWED_RESEARCH_WARNINGS | {
    "LIQUIDITY_BELOW_RESEARCH_THRESHOLD",
    "PAIR_YOUNGER_THAN_7_DAYS",
}


@dataclass(frozen=True)
class ValuationSignal:
    evidence_id: str
    observed_at: datetime
    symbol: str
    token_address: str | None
    price_usd: Decimal


@dataclass(frozen=True)
class AssetFailureState:
    contract_address: str
    symbol: str
    reason: str
    observed_at: datetime
    evidence_id: str


@dataclass(frozen=True)
class MacroState:
    available: bool
    report_id: str | None
    generated_at: datetime | None
    expires_at: datetime | None
    reason: str


@dataclass(frozen=True)
class ResearchState:
    contract_version: int
    valuation_signals: tuple[ValuationSignal, ...]
    market_candidates: tuple[ResearchSignal, ...]
    asset_failures: tuple[AssetFailureState, ...]
    macro: MacroState
    execution_authorized: bool = False

    def candidate_signals(
        self,
        *,
        macro_required: bool = False,
    ) -> tuple[ResearchSignal, ...]:
        if macro_required and not self.macro.available:
            return ()
        return self.market_candidates


def _asset_for_snapshot(
    snapshot: AcceptedMarketSnapshot,
    universe: GovernedAssetUniverse,
) -> GovernedAsset | None:
    for asset in universe.assets:
        expected_address = (asset.token_address or WETH_ADDRESS).lower()
        expected_symbol = (
            "WETH" if asset.token_address is None else asset.symbol.upper()
        )
        if (
            snapshot.contract_address == expected_address
            and snapshot.symbol.upper() == expected_symbol
        ):
            return asset
    return None


def _v2_valuation_from_packet(
    packet: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
) -> ValuationSignal:
    """Accept a fresh exact-contract v2 price without applying entry policy."""

    if not isinstance(packet, dict):
        raise ValueError("Research packet must be an object.")
    packet_id = packet.get("packet_id")
    canonical = {
        key: value
        for key, value in packet.items()
        if key not in {"packet_id", "is_stale"}
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not isinstance(packet_id, str) or packet_id != digest:
        raise ValueError("Research packet digest is invalid.")
    if (
        packet.get("schema_version") != 2
        or packet.get("network") != "base"
        or packet.get("data_quality") != "complete"
        or packet.get("recommendation") != "OBSERVE_ONLY"
        or packet.get("execution_authorized") is not False
        or packet.get("is_stale") is not False
    ):
        raise ValueError("Research packet valuation boundary is invalid.")
    try:
        observed_at = datetime.fromisoformat(str(packet.get("received_at")))
        expires_at = datetime.fromisoformat(str(packet.get("expires_at")))
    except ValueError as error:
        raise ValueError("Research packet timestamps are invalid.") from error
    if (
        observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or expires_at.tzinfo is None
        or expires_at.utcoffset() is None
    ):
        raise ValueError("Research packet timestamps must include a timezone.")
    observed_at = observed_at.astimezone(timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)
    age = (now - observed_at).total_seconds()
    if age > 120 or expires_at <= now:
        raise ValueError("Research packet is stale.")
    if age < -30:
        raise ValueError("Research packet timestamp is too far in the future.")
    contract = str(packet.get("contract_address", "")).lower()
    symbol = str(packet.get("symbol", "")).upper()
    matched_asset = next(
        (
            asset
            for asset in universe.assets
            if contract == (asset.token_address or WETH_ADDRESS).lower()
            and symbol
            == ("WETH" if asset.token_address is None else asset.symbol.upper())
        ),
        None,
    )
    if matched_asset is None:
        raise ValueError("Research token is outside the governed universe.")
    warnings = packet.get("warnings")
    if (
        not isinstance(warnings, list)
        or not ALLOWED_RESEARCH_WARNINGS.issubset(set(warnings))
        or not set(warnings).issubset(V2_VALUATION_WARNINGS)
        or len(warnings) != len(set(warnings))
    ):
        raise ValueError("Research packet contains a disallowed warning.")
    source = packet.get("source")
    if (
        not isinstance(source, dict)
        or source.get("provider") != "dexscreener"
        or source.get("discovery") != "configured_watchlist"
        or source.get("marketing_influenced") is not False
        or str(source.get("base_contract_address", "")).lower() != contract
        or str(source.get("quote_contract_address", "")).lower()
        not in {BASE_USDC_ADDRESS, WETH_ADDRESS}
    ):
        raise ValueError("Research packet source identity is invalid.")
    metrics = packet.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Research market metrics are unavailable.")
    try:
        price = Decimal(str(metrics["price_usd"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("Research valuation price is invalid.") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("Research valuation price is invalid.")
    return ValuationSignal(
        evidence_id=packet_id,
        observed_at=observed_at,
        symbol=matched_asset.symbol,
        token_address=matched_asset.token_address,
        price_usd=price,
    )


def _failure_state(failure: AcceptedAssetFailure) -> AssetFailureState:
    return AssetFailureState(
        contract_address=failure.contract_address,
        symbol=failure.symbol,
        reason=failure.reason,
        observed_at=failure.failed_at,
        evidence_id=failure.digest,
    )


def _load_v3(
    payload: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
) -> ResearchState:
    bundle = validate_research_bundle(payload, now=now)
    latest_by_asset: dict[tuple[str, str], AcceptedMarketSnapshot] = {}
    for snapshot in bundle.market_snapshots:
        identity = (snapshot.contract_address, snapshot.symbol.upper())
        previous = latest_by_asset.get(identity)
        if previous is None or snapshot.observed_at > previous.observed_at:
            latest_by_asset[identity] = snapshot
    valuations: list[ValuationSignal] = []
    candidates: list[ResearchSignal] = []
    for snapshot in latest_by_asset.values():
        asset = _asset_for_snapshot(snapshot, universe)
        price = snapshot.metrics.price_usd
        if asset is None or price is None or price <= 0:
            continue
        valuations.append(
            ValuationSignal(
                evidence_id=snapshot.digest,
                observed_at=snapshot.observed_at,
                symbol=asset.symbol,
                token_address=asset.token_address,
                price_usd=price,
            )
        )
        metrics = snapshot.metrics
        if snapshot.quality != "complete":
            continue
        if any(
            value is None
            for value in (
                metrics.liquidity_usd,
                metrics.volume_h24_usd,
                metrics.price_change_h6_percent,
                metrics.price_change_h24_percent,
                metrics.buys_h24,
                metrics.sells_h24,
            )
        ):
            continue
        candidates.append(
            ResearchSignal(
                packet_id=snapshot.digest,
                observed_at=snapshot.observed_at,
                symbol=asset.symbol,
                token_address=asset.token_address,
                price_usd=price,
                liquidity_usd=metrics.liquidity_usd,
                daily_volume_usd=metrics.volume_h24_usd,
                change_h6_percent=metrics.price_change_h6_percent,
                change_h24_percent=metrics.price_change_h24_percent,
                buys_h24=metrics.buys_h24,
                sells_h24=metrics.sells_h24,
            )
        )
    report = bundle.macro_report
    if report is None:
        macro = MacroState(False, None, None, None, "Macro report is unavailable.")
    elif report.refusal is not None:
        macro = MacroState(
            False,
            report.report_id,
            report.generated_at,
            report.expires_at,
            "Macro report contains a model refusal.",
        )
    else:
        macro = MacroState(
            True,
            report.report_id,
            report.generated_at,
            report.expires_at,
            "Current macro report is available.",
        )
    return ResearchState(
        contract_version=3,
        valuation_signals=tuple(valuations),
        market_candidates=tuple(candidates),
        asset_failures=tuple(_failure_state(item) for item in bundle.asset_failures),
        macro=macro,
    )


def _load_v2(
    payload: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
) -> ResearchState:
    if not isinstance(payload, dict) or set(payload) != V2_ENVELOPE_FIELDS:
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
    age = (now - generated_at.astimezone(timezone.utc)).total_seconds()
    if age > 120 or age < -30:
        raise ValueError("Research envelope is stale or from the future.")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("Research packets are unavailable.")
    accepted_valuations: dict[tuple[str, str | None], ValuationSignal] = {}
    accepted_candidates: dict[tuple[str, str | None], ResearchSignal] = {}
    for packet in packets:
        try:
            valuation = _v2_valuation_from_packet(packet, universe, now=now)
        except ValueError:
            valuation = None
        if valuation is not None:
            identity = (valuation.symbol, valuation.token_address)
            previous_valuation = accepted_valuations.get(identity)
            if (
                previous_valuation is None
                or valuation.observed_at > previous_valuation.observed_at
            ):
                accepted_valuations[identity] = valuation
            elif (
                valuation.observed_at == previous_valuation.observed_at
                and valuation.evidence_id != previous_valuation.evidence_id
            ):
                raise ValueError(
                    "Research valuations are ambiguous for a governed asset."
                )
        try:
            signal = research_signal_from_packet(packet, universe, now=now)
        except ValueError:
            continue
        identity = (signal.symbol, signal.token_address)
        previous = accepted_candidates.get(identity)
        if previous is None or signal.observed_at > previous.observed_at:
            accepted_candidates[identity] = signal
        elif (
            signal.observed_at == previous.observed_at
            and signal.packet_id != previous.packet_id
        ):
            raise ValueError("Research packets are ambiguous for a governed asset.")
    signals = tuple(accepted_candidates.values())
    return ResearchState(
        contract_version=2,
        valuation_signals=tuple(accepted_valuations.values()),
        market_candidates=signals,
        asset_failures=(),
        macro=MacroState(
            False,
            None,
            None,
            None,
            "Legacy version-2 research has no macro report.",
        ),
    )


def load_research_state(
    payload: object,
    universe: GovernedAssetUniverse,
    *,
    now: datetime,
) -> ResearchState:
    """Validate and normalize one v2 or v3 observation-only payload."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Research evaluation time must include a timezone.")
    current_time = now.astimezone(timezone.utc)
    version = payload.get("schema_version") if isinstance(payload, dict) else None
    if version == 2:
        return _load_v2(payload, universe, now=current_time)
    if version == 3:
        return _load_v3(payload, universe, now=current_time)
    raise ValueError("Research contract version is unsupported.")
