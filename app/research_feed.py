"""Fail-closed reader for the observation-only Railway research feed."""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_RESEARCH_URL = (
    "https://lumen-base-research-agent-production.up.railway.app"
    "/research/crypto/base/latest"
)
BASE_RESEARCH_PATH = "/research/crypto/base/latest"
ALLOWED_HOST = "lumen-base-research-agent-production.up.railway.app"
WETH_CONTRACT = "0x4200000000000000000000000000000000000006"
USDC_CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
REQUIRED_CONTRACTS = {WETH_CONTRACT, USDC_CONTRACT}
EXPECTED_SYMBOLS = {WETH_CONTRACT: "WETH", USDC_CONTRACT: "USDC"}
EXPECTED_NAMES = {WETH_CONTRACT: "Wrapped Ether", USDC_CONTRACT: "USD Coin"}
USDBC_CONTRACT = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
APPROVED_QUOTE_CONTRACTS = {
    WETH_CONTRACT: USDC_CONTRACT,
    USDC_CONTRACT: USDBC_CONTRACT,
}
REQUIRED_WARNINGS = {
    "CONTRACT_SECURITY_NOT_VERIFIED",
    "HOLDER_CONCENTRATION_NOT_VERIFIED",
}
STABLECOIN_PARTIAL_WARNINGS = REQUIRED_WARNINGS | {"MARKET_FIELDS_INCOMPLETE"}
REQUIRED_METRICS = {
    "price_usd",
    "liquidity_usd",
    "volume_h24_usd",
    "volume_h6_usd",
    "price_change_h24_percent",
    "price_change_h6_percent",
    "pair_created_at",
    "buys_h24",
    "sells_h24",
}
PACKET_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-f]{40}$")
MAX_PACKET_LIFETIME = timedelta(minutes=120)
MIN_PACKET_LIFETIME = timedelta(minutes=5)
MAX_GENERATED_AGE = timedelta(minutes=10)
MAX_FUTURE_SKEW = timedelta(seconds=30)
RESEARCH_SCHEMA_VERSION = 2
ENVELOPE_FIELDS = {
    "service", "schema_version", "mode", "execution", "generated_at", "packets",
}
PACKET_FIELDS = {
    "schema_version", "network", "contract_address", "symbol", "name",
    "pair_address", "dex_id", "received_at", "expires_at", "source", "metrics",
    "warnings", "data_quality", "recommendation", "execution_authorized",
    "packet_id", "is_stale",
}
ENRICHMENT_METRICS = {"market_cap_usd", "fdv_usd", "active_boosts"}


@dataclass(frozen=True)
class ResearchEvidence:
    ready: bool
    reason: str
    packet_ids: tuple[str, ...] = ()
    newest_received_at: datetime | None = None
    age_seconds: int | None = None
    qualities: tuple[str, ...] = ()


def _aware_timestamp(value: object, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"Research {field} is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Research {field} must include a timezone.")
    return timestamp.astimezone(timezone.utc)


def _finite_decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Research {field} is not numeric.") from error
    if not number.is_finite():
        raise ValueError(f"Research {field} must be finite.")
    return number


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Research {field} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Research {field} must be an integer.") from error
    if str(number) != str(value) or number < 0 or number > 1_000_000_000:
        raise ValueError(f"Research {field} is outside its allowed range.")
    return number


def _packet_digest(packet: dict[str, object]) -> str:
    canonical_packet = {
        key: value
        for key, value in packet.items()
        if key not in {"packet_id", "is_stale"}
    }
    encoded = json.dumps(
        canonical_packet,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_metrics(
    contract: str,
    metrics: object,
    *,
    received_at: datetime,
) -> None:
    if not isinstance(metrics, dict) or set(metrics) != REQUIRED_METRICS | ENRICHMENT_METRICS:
        raise ValueError("market metrics are incomplete or unexpected")
    missing = {field for field in REQUIRED_METRICS if metrics[field] is None}
    stablecoin_change_fields = {
        "price_change_h24_percent",
        "price_change_h6_percent",
    }
    if missing and not (
        contract == USDC_CONTRACT and missing == stablecoin_change_fields
    ):
        raise ValueError("market metrics contain null values")

    price = _finite_decimal(metrics["price_usd"], "price_usd")
    liquidity = _finite_decimal(metrics["liquidity_usd"], "liquidity_usd")
    volume_h24 = _finite_decimal(metrics["volume_h24_usd"], "volume_h24_usd")
    volume_h6 = _finite_decimal(metrics["volume_h6_usd"], "volume_h6_usd")
    change_h24 = (
        None
        if metrics["price_change_h24_percent"] is None
        else _finite_decimal(
            metrics["price_change_h24_percent"],
            "price_change_h24_percent",
        )
    )
    change_h6 = (
        None
        if metrics["price_change_h6_percent"] is None
        else _finite_decimal(
            metrics["price_change_h6_percent"],
            "price_change_h6_percent",
        )
    )
    _nonnegative_integer(metrics["buys_h24"], "buys_h24")
    _nonnegative_integer(metrics["sells_h24"], "sells_h24")
    _nonnegative_integer(metrics["active_boosts"], "active_boosts")
    for field in ("market_cap_usd", "fdv_usd"):
        if metrics[field] is not None and _finite_decimal(metrics[field], field) < 0:
            raise ValueError(f"Research {field} cannot be negative.")
    pair_created_at = _aware_timestamp(metrics["pair_created_at"], "pair_created_at")

    if contract == USDC_CONTRACT and not Decimal("0.95") <= price <= Decimal("1.05"):
        raise ValueError("USDC price is outside the approved sanity range")
    if contract == WETH_CONTRACT and not Decimal("50") <= price <= Decimal("100000"):
        raise ValueError("WETH price is outside the approved sanity range")
    if liquidity < Decimal("50000") or liquidity > Decimal("1000000000000"):
        raise ValueError("liquidity is outside the approved range")
    if not Decimal("0") <= volume_h6 <= volume_h24 <= Decimal("1000000000000"):
        raise ValueError("volume fields are inconsistent or outside the approved range")
    if (
        change_h24 is not None
        and change_h6 is not None
        and (abs(change_h24) > Decimal("1000") or abs(change_h6) > Decimal("1000"))
    ):
        raise ValueError("price change is outside the approved range")
    if pair_created_at > received_at + MAX_FUTURE_SKEW:
        raise ValueError("pair creation time is after packet receipt")


def _validate_packet(
    packet: object,
    contract: str,
    *,
    generated_at: datetime,
    current_time: datetime,
) -> tuple[str, datetime]:
    if not isinstance(packet, dict):
        raise ValueError("packet is not an object")
    if set(packet) != PACKET_FIELDS:
        raise ValueError("packet fields do not match the strict research contract")
    received_at = _aware_timestamp(packet.get("received_at"), "received_at")
    expires_at = _aware_timestamp(packet.get("expires_at"), "expires_at")
    packet_id = str(packet.get("packet_id", ""))
    source = packet.get("source")
    warnings = packet.get("warnings")

    if (
        type(packet.get("schema_version")) is not int
        or packet.get("schema_version") != RESEARCH_SCHEMA_VERSION
    ):
        raise ValueError("packet schema is not approved")
    if not PACKET_ID_PATTERN.fullmatch(packet_id) or packet_id != _packet_digest(packet):
        raise ValueError("packet digest does not match its contents")
    if (
        packet.get("network") != "base"
        or str(packet.get("contract_address", "")).lower() != contract
    ):
        raise ValueError("network or contract identity is invalid")
    if packet.get("symbol") != EXPECTED_SYMBOLS[contract]:
        raise ValueError("token symbol does not match the approved contract")
    if packet.get("name") != EXPECTED_NAMES[contract]:
        raise ValueError("token name does not match the approved contract")
    if not isinstance(packet.get("dex_id"), str) or not packet.get("dex_id"):
        raise ValueError("DEX identity is unavailable")
    pair_address = str(packet.get("pair_address", "")).lower()
    if not ADDRESS_PATTERN.fullmatch(pair_address):
        raise ValueError("pair address is invalid")
    if not isinstance(source, dict) or set(source) != {
        "provider", "discovery", "profile_url", "marketing_influenced",
        "promotion_type", "eligible_pair_count", "base_contract_address",
        "quote_contract_address", "pair_created_at_provider",
    }:
        raise ValueError("research source fields do not match the strict contract")
    if (
        source.get("provider") != "dexscreener"
        or source.get("discovery") != "configured_watchlist"
        or source.get("profile_url") is not None
        or source.get("marketing_influenced") is not False
        or source.get("promotion_type") is not None
        or source.get("pair_created_at_provider") not in {
            "dexscreener",
            "geckoterminal_governed_universe",
        }
    ):
        raise ValueError("research provider or discovery source is not approved")
    if (
        str(source.get("base_contract_address", "")).lower() != contract
        or str(source.get("quote_contract_address", "")).lower()
        != APPROVED_QUOTE_CONTRACTS[contract]
    ):
        raise ValueError("research pool asset identities are not approved")
    if _nonnegative_integer(source.get("eligible_pair_count"), "eligible_pair_count") < 1:
        raise ValueError("research source did not compare an eligible Base pool")
    stablecoin_identity_only = (
        contract == USDC_CONTRACT
        and isinstance(packet.get("metrics"), dict)
        and packet["metrics"].get("price_change_h24_percent") is None
        and packet["metrics"].get("price_change_h6_percent") is None
    )
    expected_quality = "partial" if stablecoin_identity_only else "complete"
    expected_warnings = (
        STABLECOIN_PARTIAL_WARNINGS
        if stablecoin_identity_only
        else REQUIRED_WARNINGS
    )
    if packet.get("data_quality") != expected_quality:
        raise ValueError(f"data_quality must be {expected_quality}")
    if (
        not isinstance(warnings, list)
        or any(not isinstance(warning, str) for warning in warnings)
        or warnings != sorted(expected_warnings)
    ):
        raise ValueError("packet contains a disallowed warning")
    if (
        packet.get("recommendation") != "OBSERVE_ONLY"
        or packet.get("execution_authorized") is not False
    ):
        raise ValueError("research attempted to exceed observation-only authority")
    if packet.get("is_stale") is not False:
        raise ValueError("packet staleness state is invalid")
    if received_at > current_time + MAX_FUTURE_SKEW or received_at > generated_at + MAX_FUTURE_SKEW:
        raise ValueError("packet receipt time is in the future")
    if expires_at <= current_time:
        raise ValueError("packet has expired")
    lifetime = expires_at - received_at
    if lifetime < MIN_PACKET_LIFETIME or lifetime > MAX_PACKET_LIFETIME:
        raise ValueError("packet lifetime is outside the approved bound")
    _validate_metrics(contract, packet.get("metrics"), received_at=received_at)
    return packet_id, received_at


def get_research_payload(required_contracts: tuple[str, ...] = ()) -> object:
    url = os.getenv("RESEARCH_FEED_URL", DEFAULT_RESEARCH_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError("Research feed must use the approved Railway HTTPS host.")
    if parsed.path != BASE_RESEARCH_PATH or parsed.query or parsed.fragment:
        raise ValueError(f"Research feed path must be {BASE_RESEARCH_PATH}.")
    if len(required_contracts) > 50:
        raise ValueError("Research coverage cannot exceed 50 exact contracts.")
    normalized = tuple(
        dict.fromkeys(contract.strip().lower() for contract in required_contracts)
    )
    if any(
        len(contract) != 42
        or not contract.startswith("0x")
        or any(character not in "0123456789abcdef" for character in contract[2:])
        for contract in normalized
    ):
        raise ValueError("Research coverage contains an invalid Base contract.")
    timeout = int(os.getenv("RESEARCH_FEED_TIMEOUT_SECONDS", "120"))
    if not 5 <= timeout <= 120:
        raise ValueError("RESEARCH_FEED_TIMEOUT_SECONDS must be between 5 and 120.")

    def fetch(contract: str | None) -> object:
        request_url = url
        if contract is not None:
            request_url = f"{url}?{urlencode({'required_contracts': contract})}"
        request = Request(
            request_url,
            headers={"User-Agent": "lumen-trading-monitor/2"},
        )
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.load(response)

    if not normalized:
        return fetch(None)
    payloads: dict[str, object] = {}
    failures: list[Exception] = []
    with ThreadPoolExecutor(max_workers=min(8, len(normalized))) as executor:
        futures = {contract: executor.submit(fetch, contract) for contract in normalized}
        for contract in normalized:
            try:
                payloads[contract] = futures[contract].result()
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                failures.append(error)
    if not payloads:
        raise failures[0]
    merged_packets = []
    generated_at = None
    for contract in normalized:
        if contract not in payloads:
            continue
        payload = payloads[contract]
        if not isinstance(payload, dict) or not isinstance(payload.get("packets"), list):
            raise ValueError("Research response is not a mergeable envelope.")
        if (
            payload.get("service") != "lumen-base-research-agent"
            or payload.get("schema_version") != RESEARCH_SCHEMA_VERSION
            or payload.get("mode") != "observation_only"
            or payload.get("execution") != "disabled"
        ):
            raise ValueError("Research response authority is not mergeable.")
        timestamp = _aware_timestamp(payload.get("generated_at"), "generated_at")
        generated_at = timestamp if generated_at is None else max(generated_at, timestamp)
        merged_packets.extend(
            packet
            for packet in payload["packets"]
            if isinstance(packet, dict)
            and str(packet.get("contract_address", "")).lower() == contract
        )
    if generated_at is None:
        raise ValueError("Research responses did not include a generation time.")
    return {
        "service": "lumen-base-research-agent",
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "mode": "observation_only",
        "execution": "disabled",
        "generated_at": generated_at.isoformat(),
        "packets": merged_packets,
    }


def evaluate_research_payload(
    payload: object,
    *,
    now: datetime | None = None,
) -> ResearchEvidence:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        return ResearchEvidence(False, "Research evaluation time lacks a timezone.")
    current_time = current_time.astimezone(timezone.utc)
    if not isinstance(payload, dict):
        return ResearchEvidence(False, "Research response is not an object.")
    if set(payload) != ENVELOPE_FIELDS:
        return ResearchEvidence(False, "Research response fields do not match the strict contract.")
    if (
        payload.get("service") != "lumen-base-research-agent"
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != RESEARCH_SCHEMA_VERSION
        or payload.get("mode") != "observation_only"
        or payload.get("execution") != "disabled"
    ):
        return ResearchEvidence(False, "Research service boundary is invalid.")
    try:
        generated_at = _aware_timestamp(payload.get("generated_at"), "generated_at")
    except ValueError as error:
        return ResearchEvidence(False, str(error))
    if generated_at > current_time + MAX_FUTURE_SKEW:
        return ResearchEvidence(False, "Research response generation time is in the future.")
    if current_time - generated_at > MAX_GENERATED_AGE:
        return ResearchEvidence(False, "Research response generation time is stale.")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return ResearchEvidence(False, "Research packets are unavailable.")

    selected: dict[str, dict[str, object]] = {}
    for contract in REQUIRED_CONTRACTS:
        candidates = [
            packet
            for packet in packets
            if isinstance(packet, dict)
            and str(packet.get("contract_address", "")).lower() == contract
        ]
        if not candidates:
            return ResearchEvidence(False, f"Current {EXPECTED_SYMBOLS[contract]} packet is missing.")
        try:
            received_candidates = [
                (
                    _aware_timestamp(packet.get("received_at"), "received_at"),
                    packet,
                )
                for packet in candidates
            ]
        except ValueError as error:
            return ResearchEvidence(False, str(error))
        newest_time = max(received_at for received_at, _ in received_candidates)
        newest_packets = [
            packet
            for received_at, packet in received_candidates
            if received_at == newest_time
        ]
        if len({_packet_digest(packet) for packet in newest_packets}) != 1:
            return ResearchEvidence(
                False,
                f"Current {EXPECTED_SYMBOLS[contract]} packet is ambiguous.",
            )
        selected[contract] = newest_packets[0]

    accepted: dict[str, tuple[str, datetime]] = {}
    for contract, packet in selected.items():
        try:
            accepted[contract] = _validate_packet(
                packet,
                contract,
                generated_at=generated_at,
                current_time=current_time,
            )
        except ValueError as error:
            return ResearchEvidence(
                False,
                f"Research policy rejected {EXPECTED_SYMBOLS[contract]}: {error}.",
            )

    newest = max(received_at for _, received_at in accepted.values())
    oldest = min(received_at for _, received_at in accepted.values())
    return ResearchEvidence(
        True,
        "Fresh authenticated observation-only research evidence passed.",
        tuple(sorted(packet_id for packet_id, _ in accepted.values())),
        newest,
        max(int((current_time - oldest).total_seconds()), 0),
        tuple(
            "stablecoin_identity_only"
            if selected[contract].get("data_quality") == "partial"
            else "complete"
            for contract in sorted(accepted)
        ),
    )


def load_research_evidence() -> ResearchEvidence:
    try:
        return evaluate_research_payload(get_research_payload())
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        return ResearchEvidence(False, f"Research feed unavailable: {type(error).__name__}.")
