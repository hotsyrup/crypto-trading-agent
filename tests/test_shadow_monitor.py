import json
import os
import threading
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.paper_execution import PaperOrder
from app.paper_portfolio import PaperPortfolio
from app.risk_accounting import RiskAccountingDecision
from app.safety_gate import SafetyGateDecision
from app.shadow_monitor import (
    HealthHandler,
    STATE,
    TimedHTTPServer,
    public_health_state,
    run_shadow_cycle,
    validate_execution_boundary,
)
from app.strategy import Signal


class ShadowMonitorTests(unittest.TestCase):
    observed_at = datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc)

    def common(self):
        proposal = SimpleNamespace(
            signal=Signal.BUY,
            reference_price=Decimal("2000"),
            maximum_risk=Decimal("50"),
            market_data_observed_at=self.observed_at,
            market_data_received_at=self.observed_at,
        )
        research = SimpleNamespace(
            ready=True,
            reason="Research passed.",
            packet_ids=("a" * 64, "b" * 64),
            age_seconds=20,
            qualities=("complete", "complete"),
        )
        safety = SafetyGateDecision(
            allowed=True,
            reason="Paper safety gate passed.",
            kill_switch_state="armed",
            market_data_age_seconds=30,
        )
        accounting = RiskAccountingDecision(
            ready=True,
            reason="Portfolio risk accounting passed.",
            current_value=Decimal("10000"),
            high_water_mark=Decimal("10000"),
            daily_start_value=Decimal("10000"),
            drawdown_percent=Decimal("0"),
            daily_loss_percent=Decimal("0"),
            daily_date=date(2026, 8, 12),
        )
        return proposal, research, safety, accounting

    def ledger_result(self, *, eligible, simulated, status="BLOCKED", duplicate=False):
        portfolio_after = (
            {"usdc_balance": "9949.975", "eth_balance": "0.024975"}
            if simulated
            else {"usdc_balance": "10000", "eth_balance": "0"}
        )
        entry = {
            "sequence": 1,
            "entry_hash": "c" * 64,
            "paper_eligible": eligible,
            "simulated": simulated,
            "blocked_reason": "" if eligible else "blocked",
            "order": {"status": status},
            "portfolio_after": portfolio_after,
            "simulated_value_after": "9999.975" if simulated else "10000",
            "simulated_pnl_after": "-0.025" if simulated else "0",
        }
        summary = {
            "credited_cycles": int(eligible),
            "unique_eligible_signals": int(eligible),
            "consecutive_qualifying_utc_days": 0,
            "complete": False,
            "completion_reason": None,
        }
        return entry, summary, duplicate

    def test_eligible_cycle_commits_only_simulated_portfolio(self):
        proposal, research, safety, accounting = self.common()
        portfolio = PaperPortfolio(Decimal("10000"), Decimal("0"))
        updated = PaperPortfolio(Decimal("9949.975"), Decimal("0.024975"))
        order = PaperOrder(
            Signal.BUY,
            Decimal("2000"),
            Decimal("50"),
            Decimal("0.024975"),
            "SIMULATED",
        )
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch("app.shadow_monitor.evaluate_portfolio_risk", return_value=accounting),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=True),
            patch("app.shadow_monitor.simulate_order", return_value=order),
            patch("app.shadow_monitor.apply_order", return_value=updated),
            patch("app.shadow_monitor.commit_cycle", return_value=self.ledger_result(eligible=True, simulated=True, status="SIMULATED")) as commit,
            patch("app.shadow_monitor.record_decision") as record,
            patch("app.shadow_monitor._write_operator_status"),
            patch("app.shadow_monitor.report_is_due", return_value=False),
        ):
            run_shadow_cycle()

        payload = commit.call_args.args[0]
        self.assertEqual(payload["portfolio_before"]["usdc_balance"], "10000")
        self.assertEqual(payload["portfolio_after"]["usdc_balance"], "9949.975")
        self.assertTrue(payload["paper_eligible"])
        record.assert_called_once()
        self.assertTrue(STATE["paper_eligible"])

    def test_credit_freeze_blocks_simulation_and_acceptance(self):
        proposal, research, safety, accounting = self.common()
        portfolio = PaperPortfolio(Decimal("10000"), Decimal("0"))
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch("app.shadow_monitor.evaluate_portfolio_risk", return_value=accounting),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=False),
            patch("app.shadow_monitor.simulate_order") as simulate,
            patch("app.shadow_monitor.commit_cycle", return_value=self.ledger_result(eligible=False, simulated=False)) as commit,
            patch("app.shadow_monitor.record_decision"),
            patch("app.shadow_monitor._write_operator_status"),
            patch("app.shadow_monitor.report_is_due", return_value=False),
        ):
            run_shadow_cycle()

        simulate.assert_not_called()
        payload = commit.call_args.args[0]
        self.assertFalse(payload["acceptance_credit_enabled"])
        self.assertIn("credit is frozen", payload["blocked_reason"])

    def test_duplicate_cycle_does_not_duplicate_trade_journal(self):
        proposal, research, safety, accounting = self.common()
        portfolio = PaperPortfolio(Decimal("10000"), Decimal("0"))
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch("app.shadow_monitor.evaluate_portfolio_risk", return_value=accounting),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=False),
            patch("app.shadow_monitor.commit_cycle", return_value=self.ledger_result(eligible=False, simulated=False, duplicate=True)),
            patch("app.shadow_monitor.record_decision") as record,
            patch("app.shadow_monitor._write_operator_status"),
            patch("app.shadow_monitor.report_is_due", return_value=False),
        ):
            run_shadow_cycle()
        record.assert_not_called()
        self.assertTrue(STATE["paper_cycle_duplicate"])

    def test_health_endpoint_returns_only_public_fields(self):
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        STATE.update(
            mode="monitoring_only",
            status="healthy",
            last_cycle_at="2026-08-10T17:00:00+00:00",
            signal="SELL",
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

    def test_public_health_state_is_privacy_minimized(self):
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        STATE.update(mode="monitoring_only", status="healthy", last_cycle_at="now", signal="BUY")
        self.assertEqual(
            public_health_state(),
            {
                "service": "crypto-trading-agent",
                "schema_version": 1,
                "mode": "monitoring_only",
                "status": "healthy",
                "last_cycle_at": "now",
            },
        )

    @patch("http.server.HTTPServer.get_request")
    def test_health_server_times_out_slow_clients(self, get_request):
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

    def test_live_trading_and_execution_modes_are_rejected(self):
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must remain false"):
                validate_execution_boundary()
        with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True):
            with self.assertRaisesRegex(ValueError, "monitoring_only"):
                validate_execution_boundary()


if __name__ == "__main__":
    unittest.main()
