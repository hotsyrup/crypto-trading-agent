from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.base_mcp_canary import load_base_mcp_canary_config
from app.live_trading_config import load_live_trading_config
from app.paper_acceptance import acceptance_credit_enabled, legacy_progress_status
from app.paper_cycle_ledger import (
    ACCEPTANCE_POLICY_VERSION,
    STRATEGY_ID,
    STRATEGY_VERSION,
    ledger_status,
    make_signal_id,
)
from app.paper_execution import simulate_order
from app.paper_portfolio import load_portfolio
from app.paper_portfolio import apply_order
from app.risk_accounting import portfolio_risk_transaction
from app.research_feed import load_research_evidence
from app.safety_gate import evaluate_safety_gate
from app.telegram_reporter import report_is_due, send_daily_report
from app.trade_journal import record_decision
from app.trading_cycle import create_trade_proposal
from app.strategy import Signal


STATE: dict[str, object] = {
    "mode": "monitoring_only",
    "status": "starting",
    "last_cycle_at": None,
    "last_error": None,
}
BOOT_ID = str(uuid.uuid4())
OPERATOR_STATUS_PATH = Path("data/operator_status_v2.json")

PUBLIC_HEALTH_FIELDS = ("mode", "status", "last_cycle_at")


def public_health_state() -> dict[str, object]:
    """Return only the non-sensitive fields safe for an unauthenticated probe."""
    return {
        "service": "crypto-trading-agent",
        "schema_version": 1,
        **{field: STATE.get(field) for field in PUBLIC_HEALTH_FIELDS},
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _portfolio_payload(portfolio) -> dict[str, str]:
    return {
        "usdc_balance": str(portfolio.usdc_balance),
        "eth_balance": str(portfolio.eth_balance),
    }


def base_mcp_canary_boundary_status() -> dict[str, object]:
    config = load_base_mcp_canary_config()
    return {
        "mode": config.mode,
        "kill_switch_state": config.kill_switch_state,
        "maximum_notional_usdc": str(config.maximum_notional_usdc),
        "approval_ttl_seconds": config.approval_ttl_seconds,
        "live_route": False,
        "approval_requested": False,
        "signing_authority": "base_account_human_only",
    }


def _persist_operator_report(report: dict[str, object]) -> None:
    """Atomically replace the persisted operator report."""
    OPERATOR_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OPERATOR_STATUS_PATH.parent,
            prefix=".operator_status_v2.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(report, handle, sort_keys=True, indent=2)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(OPERATOR_STATUS_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_operator_status() -> None:
    """Persist a privacy-safe report available through authenticated Railway access."""
    _persist_operator_report(
        {
            "schema_version": 2,
            "report_status": "complete",
            "paper_only": True,
            "live_route": False,
            "signing_authority": "none",
            "boot_id": BOOT_ID,
            "deployed_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unavailable"),
            "state": STATE,
            "base_mcp_canary": base_mcp_canary_boundary_status(),
            "ledger": ledger_status(),
            "legacy_acceptance": legacy_progress_status(),
        }
    )


def _write_failure_operator_status() -> None:
    """Persist minimal failure state without consulting failed cycle dependencies."""
    _persist_operator_report(
        {
            "schema_version": 2,
            "report_status": "failure_fallback",
            "paper_only": True,
            "live_route": False,
            "signing_authority": "none",
            "boot_id": BOOT_ID,
            "deployed_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unavailable"),
            "state": {
                "mode": STATE.get("mode", "monitoring_only"),
                "status": "failed",
                "last_cycle_at": STATE.get("last_cycle_at"),
                "last_error": STATE.get("last_error"),
            },
            "base_mcp_canary": {"status": "unavailable"},
            "ledger": {"status": "unavailable"},
            "legacy_acceptance": {"status": "unavailable"},
        }
    )


def _record_cycle_failure(error: Exception) -> None:
    """Expose a failed cycle in memory and best-effort persisted operator state."""
    STATE.update(
        status="failed",
        last_error=type(error).__name__,
        operator_status_write_error=None,
    )
    try:
        _write_failure_operator_status()
    except Exception as persistence_error:  # keep health endpoint alive for inspection
        STATE.update(
            operator_status_write_error=type(persistence_error).__name__,
        )
    print(json.dumps(STATE), flush=True)


def validate_execution_boundary() -> int:
    mode = os.getenv("TRADING_MODE", "monitoring_only").strip().lower()
    if mode != "monitoring_only":
        raise ValueError("TRADING_MODE must remain monitoring_only.")
    if load_live_trading_config().enabled:
        raise ValueError("LIVE_TRADING_ENABLED must remain false.")
    load_base_mcp_canary_config()
    interval = int(os.getenv("MONITOR_INTERVAL_SECONDS", "3600"))
    if interval < 300 or interval > 86400:
        raise ValueError("MONITOR_INTERVAL_SECONDS must be between 300 and 86400.")
    return interval


def run_shadow_cycle() -> None:
    recorded_at = datetime.now(timezone.utc)
    proposal = create_trade_proposal()
    research = load_research_evidence()
    safety_gate = evaluate_safety_gate(proposal)
    signal_id = make_signal_id(
        signal=proposal.signal.value,
        reference_price=proposal.reference_price,
        market_data_observed_at=proposal.market_data_observed_at,
    )
    signal_evidence = {
        "cycle_id": signal_id,
        "signal_id": signal_id,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
        "signal": proposal.signal.value,
        "reference_price": str(proposal.reference_price),
        "maximum_risk": str(proposal.maximum_risk),
        "market_data_observed_at": proposal.market_data_observed_at.isoformat(),
        "market_data_received_at": (
            proposal.market_data_received_at.isoformat()
            if proposal.market_data_received_at is not None
            else None
        ),
        "market_data_age_seconds": safety_gate.market_data_age_seconds,
        "kill_switch_state": safety_gate.kill_switch_state,
        "safety_allowed": safety_gate.allowed,
        "safety_reason": safety_gate.reason,
        "research_ready": research.ready,
        "research_reason": research.reason,
        "research_packet_ids": list(research.packet_ids),
        "research_age_seconds": research.age_seconds,
        "research_qualities": list(research.qualities),
    }
    portfolio = load_portfolio()
    with portfolio_risk_transaction(
        portfolio,
        proposal.reference_price,
        now=recorded_at,
        signal_evidence=signal_evidence,
    ) as risk_transaction:
        accounting = risk_transaction.decision
        accounting_allowed = (
            accounting.ready
            and not accounting.drawdown_halt
            and not (accounting.daily_loss_halt and proposal.signal == Signal.BUY)
        )
        position_allowed = (
            (proposal.signal == Signal.BUY and portfolio.eth_balance == 0)
            or (proposal.signal == Signal.SELL and portfolio.eth_balance > 0)
        )
        system_healthy = (
            safety_gate.allowed
            and accounting_allowed
            and research.ready
        )
        credit_enabled = acceptance_credit_enabled()
        paper_eligible = (
            system_healthy
            and position_allowed
            and credit_enabled
        )
        simulated = False
        order_status = "BLOCKED"
        order = None
        updated_portfolio = portfolio
        if paper_eligible:
            order = simulate_order(proposal)
            try:
                updated_portfolio = apply_order(portfolio, order)
                if updated_portfolio != portfolio:
                    simulated = True
                order_status = order.status
            except ValueError as error:
                paper_eligible = False
                order_status = f"REJECTED: {error}"
        blocked_reason = "; ".join(
            reason
            for ready, reason in (
                (safety_gate.allowed, safety_gate.reason),
                (accounting_allowed, accounting.reason),
                (research.ready, research.reason),
                (position_allowed, "Signal is HOLD or does not reduce/open the expected position."),
                (credit_enabled, "Corrected paper acceptance credit is frozen pending review."),
            )
            if not ready
        ) or (order_status if not paper_eligible else "")
        order_payload = {
            "status": order_status,
            "side": order.side.value if order is not None else proposal.signal.value,
            "amount_usdc": str(order.amount_usdc) if order is not None else "0",
            "quantity_eth": str(order.quantity_eth) if order is not None else "0",
            "execution_price": _decimal_text(order.execution_price) if order is not None else None,
            "fee_usdc": str(order.fee_usdc) if order is not None else "0",
        }
        simulated_value_after = (
            updated_portfolio.usdc_balance
            + updated_portfolio.eth_balance * proposal.reference_price
        )
        simulated_pnl_after = simulated_value_after - Decimal("10000.00")
        ledger_entry, acceptance, duplicate = risk_transaction.commit_cycle(
            {
                "recorded_at": recorded_at.isoformat(),
                "cycle_id": signal_id,
                "signal_id": signal_id,
                "strategy_id": STRATEGY_ID,
                "strategy_version": STRATEGY_VERSION,
                "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
                "acceptance_credit_enabled": credit_enabled,
                "paper_only": True,
                "live_route": False,
                "signal": proposal.signal.value,
                "reference_price": str(proposal.reference_price),
                "maximum_risk": str(proposal.maximum_risk),
                "market_data_observed_at": proposal.market_data_observed_at.isoformat(),
                "market_data_received_at": (
                    proposal.market_data_received_at.isoformat()
                    if proposal.market_data_received_at is not None
                    else None
                ),
                "market_data_age_seconds": safety_gate.market_data_age_seconds,
                "kill_switch_state": safety_gate.kill_switch_state,
                "safety_allowed": safety_gate.allowed,
                "safety_reason": safety_gate.reason,
                "research_ready": research.ready,
                "research_reason": research.reason,
                "research_packet_ids": list(research.packet_ids),
                "research_age_seconds": research.age_seconds,
                "research_qualities": list(research.qualities),
                "accounting_ready": accounting.ready,
                "accounting_reason": accounting.reason,
                "portfolio_value": _decimal_text(accounting.current_value),
                "high_water_mark": _decimal_text(accounting.high_water_mark),
                "daily_start_value": _decimal_text(accounting.daily_start_value),
                "drawdown_percent": _decimal_text(accounting.drawdown_percent),
                "daily_loss_percent": _decimal_text(accounting.daily_loss_percent),
                "accounting_date": (
                    accounting.daily_date.isoformat()
                    if accounting.daily_date is not None
                    else None
                ),
                "position_allowed": position_allowed,
                "system_healthy": system_healthy,
                "paper_eligible": paper_eligible,
                "simulated": simulated,
                "blocked_reason": blocked_reason,
                "order": order_payload,
                "portfolio_before": _portfolio_payload(portfolio),
                "portfolio_after": _portfolio_payload(updated_portfolio),
                "simulated_value_after": str(simulated_value_after),
                "simulated_pnl_after": str(simulated_pnl_after),
            }
        )
    if duplicate:
        order_status = str(ledger_entry["order"]["status"])
        simulated = bool(ledger_entry["simulated"])
        paper_eligible = bool(ledger_entry["paper_eligible"])
        blocked_reason = str(ledger_entry["blocked_reason"])
    else:
        record_decision(
            signal=proposal.signal,
            reference_price=proposal.reference_price,
            maximum_risk=proposal.maximum_risk,
            paper_only=True,
            risk_approved=paper_eligible,
            risk_reason=blocked_reason or "All paper eligibility controls passed.",
            order_status=order_status,
            market_data_observed_at=proposal.market_data_observed_at,
            market_data_received_at=proposal.market_data_received_at,
            safety_gate_allowed=safety_gate.allowed,
            safety_gate_reason=safety_gate.reason,
            kill_switch_state=safety_gate.kill_switch_state,
            market_data_age_seconds=safety_gate.market_data_age_seconds,
            accounting_ready=accounting.ready,
            accounting_reason=accounting.reason,
            portfolio_value=accounting.current_value,
            high_water_mark=accounting.high_water_mark,
            daily_start_value=accounting.daily_start_value,
            drawdown_percent=accounting.drawdown_percent,
            daily_loss_percent=accounting.daily_loss_percent,
            accounting_date=(
                accounting.daily_date.isoformat()
                if accounting.daily_date is not None
                else None
            ),
        )
    STATE.update(
        status="healthy",
        last_cycle_at=datetime.now(timezone.utc).isoformat(),
        last_error=None,
        signal=proposal.signal.value,
        reference_price=str(proposal.reference_price),
        safety_status="ready" if safety_gate.allowed else "blocked",
        safety_reason=safety_gate.reason,
        kill_switch_state=safety_gate.kill_switch_state,
        market_data_age_seconds=safety_gate.market_data_age_seconds,
        accounting_status="ready" if accounting.ready else "blocked",
        accounting_reason=accounting.reason,
        portfolio_value=(
            str(accounting.current_value)
            if accounting.current_value is not None
            else None
        ),
        high_water_mark=(
            str(accounting.high_water_mark)
            if accounting.high_water_mark is not None
            else None
        ),
        drawdown_percent=(
            str(accounting.drawdown_percent)
            if accounting.drawdown_percent is not None
            else None
        ),
        daily_loss_percent=(
            str(accounting.daily_loss_percent)
            if accounting.daily_loss_percent is not None
            else None
        ),
        research_status="ready" if research.ready else "blocked",
        research_reason=research.reason,
        research_packet_ids=list(research.packet_ids),
        research_age_seconds=research.age_seconds,
        research_qualities=list(research.qualities),
        paper_eligible=paper_eligible,
        paper_order_status=order_status,
        paper_order=ledger_entry["order"],
        paper_simulated=simulated,
        paper_portfolio_after=ledger_entry["portfolio_after"],
        paper_simulated_value_after=ledger_entry["simulated_value_after"],
        paper_simulated_pnl_after=ledger_entry["simulated_pnl_after"],
        paper_blocked_reason=blocked_reason,
        paper_cycle_id=signal_id,
        paper_cycle_duplicate=duplicate,
        paper_acceptance_credit_enabled=credit_enabled,
        paper_acceptance_credited_cycles=acceptance["credited_cycles"],
        paper_acceptance_unique_eligible_signals=acceptance["unique_eligible_signals"],
        paper_acceptance_consecutive_days=acceptance["consecutive_qualifying_utc_days"],
        paper_acceptance_complete=acceptance["complete"],
        paper_acceptance_completion_reason=acceptance["completion_reason"],
        ledger_sequence=ledger_entry["sequence"],
        ledger_head=ledger_entry["entry_hash"],
        operator_status_path=str(OPERATOR_STATUS_PATH),
    )
    _write_operator_status()
    print(json.dumps(STATE), flush=True)

    now = datetime.now(timezone.utc)
    if report_is_due(now):
        try:
            send_daily_report(STATE, now)
            STATE.update(telegram_report_status="sent", telegram_report_error=None)
        except Exception as error:  # reporting must not stop cloud monitoring
            STATE.update(
                telegram_report_status="failed",
                telegram_report_error=type(error).__name__,
            )
        print(
            json.dumps(
                {
                    "telegram_daily_report": STATE.get("telegram_report_status"),
                    "error": STATE.get("telegram_report_error"),
                }
            ),
            flush=True,
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        payload = json.dumps(public_health_state()).encode("utf-8")
        self.send_response(200 if STATE["status"] != "failed" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class TimedHTTPServer(HTTPServer):
    """Serve one bounded health request at a time with slow-client protection."""

    request_timeout_seconds = 5.0

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address


def serve_health() -> TimedHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    # Railway's private ingress requires the container process to bind all
    # interfaces; the service exposes only non-sensitive health state.
    server = TimedHTTPServer(("0.0.0.0", port), HealthHandler)  # nosec B104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    interval = validate_execution_boundary()
    serve_health()
    print(
        json.dumps(
            {
                "event": "paper_monitor_started",
                "boot_id": BOOT_ID,
                "paper_only": True,
                "live_route": False,
                "signing_authority": "none",
                "base_mcp_canary": base_mcp_canary_boundary_status(),
                "deployed_commit": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unavailable"),
                **ledger_status(),
                "legacy_acceptance": legacy_progress_status(),
            }
        ),
        flush=True,
    )
    while True:
        try:
            run_shadow_cycle()
        except Exception as error:  # keep health endpoint alive for inspection
            _record_cycle_failure(error)
        time.sleep(interval)


if __name__ == "__main__":
    main()
