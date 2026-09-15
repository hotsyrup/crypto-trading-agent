"""Closed version-3 contract for observation-only research resources."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


CONTRACT_VERSION = 3
NETWORK = "eip155:8453"
ENVELOPE_FIELDS = {
    "service",
    "schema_version",
    "mode",
    "execution",
    "generated_at",
    "market_snapshots",
    "asset_failures",
    "macro_report",
}
MARKET_SNAPSHOT_FIELDS = {
    "resource_type",
    "schema_version",
    "network",
    "contract_address",
    "symbol",
    "observed_at",
    "collected_at",
    "expires_at",
    "source",
    "quality",
    "metrics",
    "warnings",
    "execution_authorized",
    "digest",
}
MACRO_REPORT_FIELDS = {
    "resource_type",
    "schema_version",
    "network",
    "report_id",
    "generated_at",
    "expires_at",
    "model",
    "prompt_version",
    "source_refs",
    "findings",
    "uncertainties",
    "refusal",
    "execution_authorized",
    "digest",
}
ASSET_FAILURE_FIELDS = {
    "resource_type",
    "schema_version",
    "network",
    "contract_address",
    "symbol",
    "failed_at",
    "reason",
    "source_refs",
    "retryable",
    "execution_authorized",
    "digest",
}
MARKET_SOURCE_FIELDS = {"provider", "source_ref", "raw_response_sha256"}
MARKET_METRIC_FIELDS = {
    "price_usd",
    "liquidity_usd",
    "volume_h24_usd",
    "volume_h6_usd",
    "price_change_h24_percent",
    "price_change_h6_percent",
    "buys_h24",
    "sells_h24",
}
MACRO_SOURCE_FIELDS = {
    "provider",
    "source_ref",
    "observed_at",
    "retrieved_at",
    "raw_response_sha256",
}
MACRO_FINDING_FIELDS = {
    "metric",
    "period",
    "value",
    "unit",
    "comparison_value",
    "comparison_period",
}
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FAILURE_REASONS = {
    "provider_unavailable",
    "stale_source",
    "invalid_payload",
    "contradictory",
    "out_of_scope",
}


@dataclass(frozen=True)
class AcceptedMarketMetrics:
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    volume_h24_usd: Decimal | None
    volume_h6_usd: Decimal | None
    price_change_h24_percent: Decimal | None
    price_change_h6_percent: Decimal | None
    buys_h24: int | None
    sells_h24: int | None


@dataclass(frozen=True)
class AcceptedMarketSnapshot:
    contract_address: str
    symbol: str
    observed_at: datetime
    expires_at: datetime
    quality: str
    metrics: AcceptedMarketMetrics
    digest: str


@dataclass(frozen=True)
class AcceptedAssetFailure:
    contract_address: str
    symbol: str
    reason: str
    failed_at: datetime
    digest: str


@dataclass(frozen=True)
class AcceptedMacroReport:
    report_id: str
    generated_at: datetime
    expires_at: datetime
    refusal: str | None


@dataclass(frozen=True)
class AcceptedResearchBundle:
    schema_version: int
    generated_at: datetime
    market_snapshots: tuple[AcceptedMarketSnapshot, ...]
    asset_failures: tuple[AcceptedAssetFailure, ...]
    macro_report: AcceptedMacroReport | None
    execution_authorized: bool = False


def _timestamp(value: object, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone.")
    return timestamp.astimezone(timezone.utc)


def _resource_digest(resource: dict[str, object]) -> str:
    canonical = {key: value for key, value in resource.items() if key != "digest"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: object, field: str, *, nonnegative: bool) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string or null.")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field} is not numeric.") from error
    if not number.is_finite() or (nonnegative and number < 0):
        raise ValueError(f"{field} is not a valid numeric value.")
    return number


def validate_research_bundle(
    payload: object,
    *,
    now: datetime | None = None,
) -> AcceptedResearchBundle:
    """Validate a v3 bundle and return its normalized observation-only state."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("Research evaluation time must include a timezone.")
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        raise ValueError(
            "Legacy research contract version 2 must be handled by the version-2 adapter."
        )
    if not isinstance(payload, dict) or set(payload) != ENVELOPE_FIELDS:
        raise ValueError("Research bundle fields do not match contract version 3.")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != CONTRACT_VERSION
        or payload.get("service") != "lumen-base-research-agent"
        or payload.get("mode") != "observation_only"
        or payload.get("execution") != "disabled"
    ):
        raise ValueError("Research bundle authority or version is invalid.")
    generated_at = _timestamp(payload.get("generated_at"), "generated_at")
    snapshots = payload.get("market_snapshots")
    failures = payload.get("asset_failures")
    report = payload.get("macro_report")
    if not isinstance(snapshots, list) or not isinstance(failures, list):
        raise ValueError("Research bundle resources must be arrays.")
    accepted_snapshots: list[AcceptedMarketSnapshot] = []
    snapshot_by_observation: dict[tuple[str, datetime], str] = {}
    seen_snapshot_digests: set[str] = set()
    for item in snapshots:
        if not isinstance(item, dict) or set(item) != MARKET_SNAPSHOT_FIELDS:
            raise ValueError("Version-3 market snapshot fields do not match the closed contract.")
        if item.get("execution_authorized") is not False:
            raise ValueError("Research resource attempted to claim execution authority.")
        if (
            item.get("resource_type") != "market_snapshot"
            or item.get("schema_version") != CONTRACT_VERSION
            or item.get("network") != NETWORK
        ):
            raise ValueError("Market snapshot identity is invalid.")
        contract_address = str(item.get("contract_address", ""))
        symbol = item.get("symbol")
        if (
            not ADDRESS_PATTERN.fullmatch(contract_address)
            or not isinstance(symbol, str)
            or not symbol
        ):
            raise ValueError("Market snapshot asset identity is invalid.")
        observed_at = _timestamp(item.get("observed_at"), "market snapshot observed_at")
        collected_at = _timestamp(item.get("collected_at"), "market snapshot collected_at")
        expires_at = _timestamp(item.get("expires_at"), "market snapshot expires_at")
        if not observed_at <= collected_at <= expires_at:
            raise ValueError("Market snapshot timestamps are inconsistent.")
        if expires_at <= current_time.astimezone(timezone.utc):
            raise ValueError("Market snapshot has expired.")
        quality = item.get("quality")
        if quality not in {"complete", "partial"}:
            raise ValueError("Market snapshot quality is invalid.")
        warnings = item.get("warnings")
        if not isinstance(warnings, list) or any(
            not isinstance(warning, str) or not warning for warning in warnings
        ):
            raise ValueError("Market snapshot warnings are invalid.")
        if quality == "partial" and not warnings:
            raise ValueError("Partial market snapshot requires an explicit warning.")
        source = item.get("source")
        metrics = item.get("metrics")
        if not isinstance(source, dict) or set(source) != MARKET_SOURCE_FIELDS:
            raise ValueError("Market snapshot source fields do not match the closed contract.")
        if (
            not isinstance(source.get("provider"), str)
            or not source.get("provider")
            or not isinstance(source.get("source_ref"), str)
            or not source.get("source_ref")
            or not DIGEST_PATTERN.fullmatch(str(source.get("raw_response_sha256", "")))
        ):
            raise ValueError("Market snapshot source hash or identity is invalid.")
        if not isinstance(metrics, dict) or set(metrics) != MARKET_METRIC_FIELDS:
            raise ValueError("Market snapshot metric fields do not match the closed contract.")
        decimal_metric_fields = {
            "price_usd",
            "liquidity_usd",
            "volume_h24_usd",
            "volume_h6_usd",
            "price_change_h24_percent",
            "price_change_h6_percent",
        }
        decimal_metrics = {
            field: _decimal(
                metrics[field],
                f"Market snapshot {field}",
                nonnegative=field not in {
                    "price_change_h24_percent",
                    "price_change_h6_percent",
                },
            )
            for field in decimal_metric_fields
        }
        for field in ("buys_h24", "sells_h24"):
            value = metrics[field]
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"Market snapshot {field} is not numeric.")
        missing_metrics = {field for field, value in metrics.items() if value is None}
        if quality == "complete" and missing_metrics:
            raise ValueError("Complete market snapshot cannot contain missing metrics.")
        if quality == "partial" and not missing_metrics:
            raise ValueError("Partial market snapshot must identify missing metrics.")
        digest = str(item.get("digest", ""))
        if digest != _resource_digest(item):
            raise ValueError("Market snapshot digest does not match its content.")
        identity = (contract_address, observed_at)
        previous_digest = snapshot_by_observation.get(identity)
        if previous_digest is not None and previous_digest != digest:
            raise ValueError("Market snapshots are contradictory for the same observation.")
        snapshot_by_observation[identity] = digest
        if digest in seen_snapshot_digests:
            continue
        seen_snapshot_digests.add(digest)
        accepted_snapshots.append(
            AcceptedMarketSnapshot(
                contract_address=identity[0],
                symbol=str(item["symbol"]),
                observed_at=observed_at,
                expires_at=expires_at,
                quality=str(quality),
                metrics=AcceptedMarketMetrics(
                    price_usd=decimal_metrics["price_usd"],
                    liquidity_usd=decimal_metrics["liquidity_usd"],
                    volume_h24_usd=decimal_metrics["volume_h24_usd"],
                    volume_h6_usd=decimal_metrics["volume_h6_usd"],
                    price_change_h24_percent=decimal_metrics[
                        "price_change_h24_percent"
                    ],
                    price_change_h6_percent=decimal_metrics[
                        "price_change_h6_percent"
                    ],
                    buys_h24=metrics["buys_h24"],
                    sells_h24=metrics["sells_h24"],
                ),
                digest=digest,
            )
        )

    accepted_failures: list[AcceptedAssetFailure] = []
    for item in failures:
        if not isinstance(item, dict) or set(item) != ASSET_FAILURE_FIELDS:
            raise ValueError("Version-3 asset failure fields do not match the closed contract.")
        if item.get("execution_authorized") is not False:
            raise ValueError("Research resource attempted to claim execution authority.")
        if (
            item.get("resource_type") != "asset_failure"
            or item.get("schema_version") != CONTRACT_VERSION
            or item.get("network") != NETWORK
        ):
            raise ValueError("Asset failure identity is invalid.")
        contract_address = str(item.get("contract_address", ""))
        if not ADDRESS_PATTERN.fullmatch(contract_address):
            raise ValueError("Asset failure contract address is invalid.")
        if item.get("reason") not in FAILURE_REASONS:
            raise ValueError("Asset failure reason is invalid.")
        if type(item.get("retryable")) is not bool:
            raise ValueError("Asset failure retryable state is invalid.")
        source_refs = item.get("source_refs")
        if not isinstance(source_refs, list) or any(
            not isinstance(source_ref, str) or not source_ref
            for source_ref in source_refs
        ):
            raise ValueError("Asset failure source references are invalid.")
        digest = str(item.get("digest", ""))
        if digest != _resource_digest(item):
            raise ValueError("Asset failure digest does not match its content.")
        accepted_failures.append(
            AcceptedAssetFailure(
                contract_address=contract_address,
                symbol=str(item["symbol"]),
                reason=str(item["reason"]),
                failed_at=_timestamp(item.get("failed_at"), "asset failure failed_at"),
                digest=digest,
            )
        )
    snapshot_contracts = {snapshot.contract_address for snapshot in accepted_snapshots}
    failure_contracts = {failure.contract_address for failure in accepted_failures}
    if snapshot_contracts & failure_contracts:
        raise ValueError("An asset cannot have both snapshot and failure state.")
    accepted_report = None
    if report is not None:
        if not isinstance(report, dict) or set(report) != MACRO_REPORT_FIELDS:
            raise ValueError("Version-3 macro report fields do not match the closed contract.")
        if report.get("execution_authorized") is not False:
            raise ValueError("Research resource attempted to claim execution authority.")
        if (
            report.get("resource_type") != "macro_report"
            or report.get("schema_version") != CONTRACT_VERSION
            or report.get("network") != NETWORK
        ):
            raise ValueError("Macro report identity is invalid.")
        if not DIGEST_PATTERN.fullmatch(str(report.get("report_id", ""))):
            raise ValueError("Macro report ID must be a SHA-256 value.")
        report_generated_at = _timestamp(
            report.get("generated_at"), "macro report generated_at"
        )
        report_expires_at = _timestamp(
            report.get("expires_at"), "macro report expires_at"
        )
        if (
            report_generated_at > generated_at
            or report_expires_at <= report_generated_at
            or report_expires_at - report_generated_at > timedelta(hours=6)
        ):
            raise ValueError("Macro report timestamps are inconsistent.")
        if report_expires_at <= current_time.astimezone(timezone.utc):
            raise ValueError("Macro report has expired.")
        source_refs = report.get("source_refs")
        findings = report.get("findings")
        if not isinstance(source_refs, list) or not source_refs or any(
            not isinstance(source, dict) or set(source) != MACRO_SOURCE_FIELDS
            for source in source_refs
        ):
            raise ValueError("Macro report source fields do not match the closed contract.")
        for source in source_refs:
            if (
                not isinstance(source.get("provider"), str)
                or not source.get("provider")
                or not isinstance(source.get("source_ref"), str)
                or not source.get("source_ref")
                or not DIGEST_PATTERN.fullmatch(
                    str(source.get("raw_response_sha256", ""))
                )
            ):
                raise ValueError("Macro report source hash or identity is invalid.")
            observed_at = _timestamp(source.get("observed_at"), "macro source observed_at")
            retrieved_at = _timestamp(source.get("retrieved_at"), "macro source retrieved_at")
            if not observed_at <= retrieved_at <= report_generated_at:
                raise ValueError("Macro report source timestamps are inconsistent.")
        if not isinstance(findings, list) or any(
            not isinstance(finding, dict) or set(finding) != MACRO_FINDING_FIELDS
            for finding in findings
        ):
            raise ValueError("Macro report finding fields do not match the closed contract.")
        if str(report.get("digest", "")) != _resource_digest(report):
            raise ValueError("Macro report digest does not match its content.")
        refusal = report.get("refusal")
        if refusal is not None and (not isinstance(refusal, str) or not refusal):
            raise ValueError("Macro report refusal must be a non-empty string or null.")
        accepted_report = AcceptedMacroReport(
            report_id=str(report["report_id"]),
            generated_at=report_generated_at,
            expires_at=report_expires_at,
            refusal=refusal,
        )
    return AcceptedResearchBundle(
        schema_version=CONTRACT_VERSION,
        generated_at=generated_at,
        market_snapshots=tuple(accepted_snapshots),
        asset_failures=tuple(accepted_failures),
        macro_report=accepted_report,
    )
