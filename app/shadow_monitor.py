import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.live_trading_config import load_live_trading_config
from app.trade_journal import record_decision
from app.trading_cycle import create_trade_proposal


STATE: dict[str, object] = {
    "mode": "monitoring_only",
    "status": "starting",
    "last_cycle_at": None,
    "last_error": None,
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
    record_decision(
        signal=proposal.signal,
        reference_price=proposal.reference_price,
        maximum_risk=proposal.maximum_risk,
        paper_only=True,
        risk_approved=False,
        risk_reason="Shadow monitoring mandate: execution disabled.",
        order_status="SHADOW_ONLY",
    )
    STATE.update(
        status="healthy",
        last_cycle_at=datetime.now(timezone.utc).isoformat(),
        last_error=None,
        signal=proposal.signal.value,
        reference_price=str(proposal.reference_price),
    )
    print(json.dumps(STATE), flush=True)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        payload = json.dumps(STATE).encode("utf-8")
        self.send_response(200 if STATE["status"] != "failed" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve_health() -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    # Railway's private ingress requires the container process to bind all
    # interfaces; the service exposes only non-sensitive health state.
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)  # nosec B104
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
