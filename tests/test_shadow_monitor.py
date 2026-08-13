import os
import json
import threading
import unittest
from datetime import date
from decimal import Decimal
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from app.safety_gate import SafetyGateDecision
from app.risk_accounting import RiskAccountingDecision
from app.shadow_monitor import (
    HealthHandler,
    STATE,
    TimedHTTPServer,
    public_health_state,
    run_shadow_cycle,
    validate_execution_boundary,
)


class ShadowMonitorTests(unittest.TestCase):
    def test_health_endpoint_returns_only_public_fields(self) -> None:
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        STATE.update(
            mode="monitoring_only",
            status="healthy",
            last_cycle_at="2026-08-10T17:00:00+00:00",
            signal="SELL",
            reference_price="1234",
            portfolio_value="9999",
        )
        server = TimedHTTPServer(("127.0.0.1", 0), HealthHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        thread.join(timeout=2)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(
            payload,
            {
                "service": "crypto-trading-agent",
                "schema_version": 1,
                "mode": "monitoring_only",
                "status": "healthy",
                "last_cycle_at": "2026-08-10T17:00:00+00:00",
            },
        )

    def test_public_health_state_is_privacy_minimized(self) -> None:
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        STATE.update(
            mode="monitoring_only",
            status="healthy",
            last_cycle_at="2026-08-10T17:00:00+00:00",
            signal="BUY",
            reference_price="9999",
            portfolio_value="10000",
            safety_reason="private detail",
            last_error="private detail",
        )

        self.assertEqual(
            public_health_state(),
            {
                "service": "crypto-trading-agent",
                "schema_version": 1,
                "mode": "monitoring_only",
                "status": "healthy",
                "last_cycle_at": "2026-08-10T17:00:00+00:00",
            },
        )

    @patch("http.server.HTTPServer.get_request")
    def test_health_server_times_out_slow_clients(self, get_request) -> None:
        request = MagicMock()
        get_request.return_value = (request, ("127.0.0.1", 12345))
        server = object.__new__(TimedHTTPServer)

        accepted_request, client_address = server.get_request()

        self.assertIs(accepted_request, request)
        self.assertEqual(client_address, ("127.0.0.1", 12345))
        request.settimeout.assert_called_once_with(5.0)

    def test_default_boundary_is_monitoring_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(validate_execution_boundary(), 3600)

    def test_live_trading_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_MODE": "monitoring_only", "LIVE_TRADING_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must remain false"):
                validate_execution_boundary()

    def test_execution_mode_is_rejected(self) -> None:
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True):
            with self.assertRaisesRegex(ValueError, "monitoring_only"):
                validate_execution_boundary()

    def test_interval_is_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {"TRADING_MODE": "monitoring_only", "MONITOR_INTERVAL_SECONDS": "60"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "between 300 and 86400"):
                validate_execution_boundary()

    @patch("app.shadow_monitor.send_daily_report")
    @patch("app.shadow_monitor.report_is_due", return_value=False)
    @patch("app.shadow_monitor.load_research_evidence")
    @patch("app.shadow_monitor.record_decision")
    @patch("app.shadow_monitor.evaluate_portfolio_risk")
    @patch("app.shadow_monitor.load_portfolio")
    @patch("app.shadow_monitor.evaluate_safety_gate")
    @patch("app.shadow_monitor.create_trade_proposal")
    def test_cycle_does_not_report_when_not_due(
        self, proposal, safety_gate, load, accounting, record, research, due, send
    ) -> None:
        research.return_value.ready = False
        research.return_value.reason = "Research blocked."
        research.return_value.packet_ids = ()
        research.return_value.age_seconds = None
        proposal.return_value.signal.value = "HOLD"
        proposal.return_value.reference_price = 1
        proposal.return_value.maximum_risk = 0
        safety_gate.return_value = SafetyGateDecision(
            allowed=False,
            reason="Paper kill switch is halted.",
            kill_switch_state="halted",
        )
        accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="Portfolio risk accounting passed.",
            current_value=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("0"),
            daily_loss_percent=Decimal("0"),
            daily_date=date(2026, 8, 8),
        )
        run_shadow_cycle()
        record.assert_called_once()
        due.assert_called_once()
        send.assert_not_called()

    @patch("app.shadow_monitor.send_daily_report", side_effect=RuntimeError("offline"))
    @patch("app.shadow_monitor.report_is_due", return_value=True)
    @patch("app.shadow_monitor.load_research_evidence")
    @patch("app.shadow_monitor.record_decision")
    @patch("app.shadow_monitor.evaluate_portfolio_risk")
    @patch("app.shadow_monitor.load_portfolio")
    @patch("app.shadow_monitor.evaluate_safety_gate")
    @patch("app.shadow_monitor.create_trade_proposal")
    def test_reporting_failure_does_not_fail_monitor(
        self, proposal, safety_gate, load, accounting, record, research, due, send
    ) -> None:
        research.return_value.ready = True
        research.return_value.reason = "Research passed."
        research.return_value.packet_ids = ("a" * 64, "b" * 64)
        research.return_value.age_seconds = 30
        proposal.return_value.signal.value = "HOLD"
        proposal.return_value.reference_price = 1
        proposal.return_value.maximum_risk = 0
        safety_gate.return_value = SafetyGateDecision(
            allowed=False,
            reason="Paper kill switch is halted.",
            kill_switch_state="halted",
        )
        accounting.return_value = RiskAccountingDecision(
            ready=True,
            reason="Portfolio risk accounting passed.",
            current_value=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("0"),
            daily_loss_percent=Decimal("0"),
            daily_date=date(2026, 8, 8),
        )
        run_shadow_cycle()
        self.assertEqual(send.call_count, 1)
