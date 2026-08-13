"""Fail-closed reader for the observation-only Railway research feed."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_RESEARCH_URL = (
    "https://lumen-base-research-agent-production.up.railway.app/research/latest"
)
ALLOWED_HOST = "lumen-base-research-agent-production.up.railway.app"
REQUIRED_CONTRACTS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
}


@dataclass(frozen=True)
class ResearchEvidence:
    ready: bool
    reason: str
    packet_ids: tuple[str, ...] = ()
    newest_received_at: datetime | None = None
    age_seconds: int | None = None


def _aware_timestamp(value: object, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"Research {field} is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Research {field} must include a timezone.")
    return timestamp


def get_research_payload() -> object:
    url = os.getenv("RESEARCH_FEED_URL", DEFAULT_RESEARCH_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError("Research feed must use the approved Railway HTTPS host.")
    if parsed.path != "/research/latest" or parsed.query or parsed.fragment:
        raise ValueError("Research feed path must be /research/latest.")
    request = Request(url, headers={"User-Agent": "lumen-trading-monitor/1"})
    with urlopen(request, timeout=10) as response:  # nosec B310
        return json.load(response)


def evaluate_research_payload(
    payload: object,
    *,
    now: datetime | None = None,
) -> ResearchEvidence:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        return ResearchEvidence(False, "Research evaluation time lacks a timezone.")
    if not isinstance(payload, dict):
        return ResearchEvidence(False, "Research response is not an object.")
    if (
        payload.get("service") != "lumen-base-research-agent"
        or payload.get("schema_version") != 1
        or payload.get("mode") != "observation_only"
        or payload.get("execution") != "disabled"
    ):
        return ResearchEvidence(False, "Research service boundary is invalid.")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return ResearchEvidence(False, "Research packets are unavailable.")

    accepted: dict[str, tuple[str, datetime]] = {}
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        contract = str(packet.get("contract_address", "")).lower()
        if contract not in REQUIRED_CONTRACTS or contract in accepted:
            continue
        try:
            received_at = _aware_timestamp(packet.get("received_at"), "received_at")
            expires_at = _aware_timestamp(packet.get("expires_at"), "expires_at")
        except ValueError:
            continue
        packet_id = str(packet.get("packet_id", ""))
        if (
            len(packet_id) != 64
            or packet.get("network") != "base"
            or packet.get("recommendation") != "OBSERVE_ONLY"
            or packet.get("execution_authorized") is not False
            or packet.get("data_quality") != "complete"
            or packet.get("is_stale") is True
            or expires_at <= current_time
            or received_at > current_time
        ):
            continue
        accepted[contract] = (packet_id, received_at)

    missing = REQUIRED_CONTRACTS.difference(accepted)
    if missing:
        return ResearchEvidence(
            False,
            "Fresh complete WETH and USDC research evidence is required.",
        )
    newest = max(received_at for _, received_at in accepted.values())
    return ResearchEvidence(
        True,
        "Fresh observation-only research evidence passed.",
        tuple(sorted(packet_id for packet_id, _ in accepted.values())),
        newest,
        max(int((current_time - newest).total_seconds()), 0),
    )


def load_research_evidence() -> ResearchEvidence:
    try:
        return evaluate_research_payload(get_research_payload())
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        return ResearchEvidence(False, f"Research feed unavailable: {type(error).__name__}.")
