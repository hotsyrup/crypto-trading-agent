"""Observation-only Stage 3 orchestration and read-only resource routing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.research_v3_runtime import AssetTarget, ResearchV3Store, seal


def _failure(target: AssetTarget, now: datetime, *, retryable: bool) -> dict[str, object]:
    return seal({
        "resource_type": "asset_failure",
        "schema_version": 3,
        "network": "eip155:8453",
        "contract_address": target.contract_address,
        "symbol": target.symbol,
        "failed_at": now.isoformat(),
        "reason": "provider_unavailable" if retryable else "invalid_payload",
        "source_refs": [
            f"https://api.dexscreener.com/tokens/v1/base/{target.contract_address}"
        ],
        "retryable": retryable,
        "execution_authorized": False,
    })


@dataclass(frozen=True)
class ResearchV3Service:
    store: ResearchV3Store
    dex_collector: object
    macro_collectors: tuple[object, ...]
    synthesis: object
    targets: tuple[AssetTarget, ...]
    batch_market_collector: object | None = None

    def run_cycle(self, *, now: datetime) -> dict[str, object]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Research cycle time must include a timezone.")
        current = now.astimezone(timezone.utc)
        snapshot_count = 0
        failure_count = 0
        recovered: dict[tuple[str, str], dict[str, object]] = {}
        if self.batch_market_collector is not None:
            try:
                recovered = self.batch_market_collector.collect_many(
                    self.targets,
                    now=current,
                )
            except (TimeoutError, ConnectionError, OSError, RuntimeError, TypeError, ValueError, KeyError):
                recovered = {}
        failed_targets: list[tuple[AssetTarget, bool]] = []
        for target in self.targets:
            try:
                snapshot = self.dex_collector.collect(target, now=current)
            except (TimeoutError, ConnectionError, OSError, RuntimeError):
                failed_targets.append((target, True))
            except (TypeError, ValueError, KeyError):
                failed_targets.append((target, False))
            else:
                self.store.put_market(snapshot)
                snapshot_count += 1

        for target, retryable in failed_targets:
            snapshot = recovered.get((target.contract_address, target.symbol))
            if snapshot is None:
                self.store.put_failure(_failure(target, current, retryable=retryable))
                failure_count += 1
            else:
                self.store.put_market(snapshot)
                snapshot_count += 1

        source_facts: list[dict[str, object]] = []
        macro_failures = 0
        synthesis_due = getattr(self.synthesis, "is_due", lambda **_kwargs: True)(now=current)
        if synthesis_due:
            for collector in self.macro_collectors:
                try:
                    if hasattr(collector, "target"):
                        item = collector.collect(collector.target, now=current)
                    else:
                        item = collector.collect(now=current)
                    source_facts.append(item)
                except (TimeoutError, ConnectionError, OSError, RuntimeError, TypeError, ValueError, KeyError):
                    macro_failures += 1
        macro_report = None
        if source_facts:
            try:
                macro_report = self.synthesis.run_if_due(
                    source_facts=source_facts,
                    now=current,
                )
            except (TimeoutError, ConnectionError, OSError, RuntimeError, TypeError, ValueError, KeyError):
                macro_failures += 1
        return {
            "status": "healthy" if snapshot_count else "degraded",
            "market_snapshots": snapshot_count,
            "asset_failures": failure_count,
            "macro_sources": len(source_facts),
            "macro_failures": macro_failures,
            "macro_report_created": macro_report is not None,
            "execution_authorized": False,
        }


def _fresh_market(store: ResearchV3Store, now: datetime) -> list[dict[str, object]]:
    resources = store.latest_by_identity("market_snapshot")
    return [
        resource for resource in resources
        if datetime.fromisoformat(str(resource["expires_at"])).astimezone(timezone.utc) > now
    ]


def _current_macro(store: ResearchV3Store, now: datetime) -> dict[str, object] | None:
    report = store.latest_macro()
    if report is None:
        return None
    expires = datetime.fromisoformat(str(report["expires_at"])).astimezone(timezone.utc)
    return report if expires > now else None


def build_bundle(store: ResearchV3Store, *, now: datetime) -> dict[str, object]:
    current = now.astimezone(timezone.utc)
    snapshots = _fresh_market(store, current)
    failures_by_contract = {
        item["contract_address"]: item
        for item in store.latest_by_identity("asset_failure")
    }
    for snapshot in snapshots:
        failures_by_contract.pop(snapshot["contract_address"], None)
    return {
        "service": "lumen-base-research-agent",
        "schema_version": 3,
        "mode": "observation_only",
        "execution": "disabled",
        "generated_at": current.isoformat(),
        "market_snapshots": snapshots,
        "asset_failures": list(failures_by_contract.values()),
        "macro_report": _current_macro(store, current),
    }


def public_route_response(
    path: str,
    store: ResearchV3Store,
    *,
    now: datetime,
) -> tuple[int, dict[str, object]] | None:
    current = now.astimezone(timezone.utc)
    markets = _fresh_market(store, current)
    macro = _current_macro(store, current)
    if path in {"/", "/health"}:
        return 200, {
            "service": "lumen-base-research-agent",
            "schema_version": 3,
            "status": "healthy",
            "mode": "observation_only",
            "execution": "disabled",
            "execution_authorized": False,
            "fresh_market_snapshots": len(markets),
            "macro_report_available": macro is not None,
        }
    if path == "/ready":
        ready = bool(markets)
        return (200 if ready else 503), {
            "status": "ready" if ready else "not_ready",
            "reason": "fresh_market_snapshot_available" if ready else "no_fresh_market_snapshot",
            "execution": "disabled",
            "execution_authorized": False,
        }
    if path == "/research/v3/bundle":
        return 200, build_bundle(store, now=current)
    market_prefix = "/research/v3/market/"
    if path.startswith(market_prefix):
        contract = path[len(market_prefix):]
        market = next(
            (item for item in markets if item["contract_address"] == contract),
            None,
        )
        return (200, market) if market else (404, {
            "status": "unavailable", "execution_authorized": False
        })
    if path == "/research/v3/macro/latest":
        return (200, macro) if macro else (404, {
            "status": "unavailable", "execution_authorized": False
        })
    return None
