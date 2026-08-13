import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.live_trading_config import load_live_trading_config
from app.paper_acceptance import update_acceptance
from app.paper_execution import simulate_order
from app.paper_portfolio import load_portfolio
from app.paper_portfolio import apply_order, save_portfolio
from app.risk_accounting import evaluate_portfolio_risk
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

PUBLIC_HEALTH_FIELDS = ("mode", "status", "last_cycle_at")


def public_health_state() -> dict[str, object]:
    """Return only the non-sensitive fields safe for an unauthenticated probe."""
    return {
        "service": "crypto-trading-agent",
        "schema_version": 1,
        **{field: STATE.get(field) for field in PUBLIC_HEALTH_FIELDS},
    }


def validate_execution_boundary() -> int:
    mode = os.getenv("TRADING_MODE", "monitoring_only").strip().lower()
    if mode != "monitoring_only":
        raise ValueError("TRADING_MODE must remain monitoring_only.")
    if load_live_trading_config().enabled:
        raise ValueError("LIVE_TRADING_ENABLED must remain false.")
    interval = int(os.getenv("MONITOR_INTERVAL_SECONDS", "3600"))
    if interval < 300 or interval > 86400:
        raise ValueError("MONITOR_INTERVAL_SECONDS must be between 300 and 86400.")
    return interval


def run_shadow_cycle() -> None:
    proposal = create_trade_proposal()
    research = load_research_evidence()
    safety_gate = evaluate_safety_gate(proposal)
    portfolio = load_portfolio()
    accounting = evaluate_portfolio_risk(
        portfolio,
        proposal.reference_price,
    )
    accounting_allowed = (
        accounting.ready
        and not accounting.drawdown_halt
        and not (accounting.daily_loss_halt and proposal.signal == Signal.BUY)
    )
    position_allowed = (
        (proposal.signal == Signal.BUY and portfolio.eth_balance == 0)
        or (proposal.signal == Signal.SELL and portfolio.eth_balance > 0)
    )
    paper_eligible = (
        safety_gate.allowed
        and accounting_allowed
        and research.ready
        and position_allowed
    )
    simulated = False
    order_status = "BLOCKED"
    if paper_eligible:
        order = simulate_order(proposal)
        try:
            updated_portfolio = apply_order(portfolio, order)
            if updated_portfolio != portfolio:
                save_portfolio(updated_portfolio)
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
        )
        if not ready
    ) or (order_status if not paper_eligible else "")
    acceptance = update_acceptance(
        eligible=paper_eligible,
        simulated=simulated,
        blocked_reason=blocked_reason,
    )
    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=True,
        risk_approved=False,
        risk_reason="Shadow monitoring mandate: execution disabled.",
        order_status="SHADOW_ONLY",
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
        paper_eligible=paper_eligible,
        paper_order_status=order_status,
        paper_acceptance_cycles=acceptance["cycles"],
        paper_acceptance_eligible_cycles=acceptance["eligible_cycles"],
        paper_acceptance_simulated_orders=acceptance["simulated_orders"],
        paper_acceptance_complete=acceptance["complete"],
    )
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
    while True:
        try:
            run_shadow_cycle()
        except Exception as error:  # keep health endpoint alive for inspection
            STATE.update(status="failed", last_error=type(error).__name__)
            print(json.dumps(STATE), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
