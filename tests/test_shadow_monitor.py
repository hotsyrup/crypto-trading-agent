import os
import unittest
from unittest.mock import patch

from app.shadow_monitor import run_shadow_cycle, validate_execution_boundary


class ShadowMonitorTests(unittest.TestCase):
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
    @patch("app.shadow_monitor.record_decision")
    @patch("app.shadow_monitor.create_trade_proposal")
    def test_cycle_does_not_report_when_not_due(
        self, proposal, record, due, send
    ) -> None:
        proposal.return_value.signal.value = "HOLD"
        proposal.return_value.reference_price = 1
        proposal.return_value.maximum_risk = 0
        run_shadow_cycle()
        record.assert_called_once()
        due.assert_called_once()
        send.assert_not_called()

    @patch("app.shadow_monitor.send_daily_report", side_effect=RuntimeError("offline"))
    @patch("app.shadow_monitor.report_is_due", return_value=True)
    @patch("app.shadow_monitor.record_decision")
    @patch("app.shadow_monitor.create_trade_proposal")
    def test_reporting_failure_does_not_fail_monitor(
        self, proposal, record, due, send
    ) -> None:
        proposal.return_value.signal.value = "HOLD"
        proposal.return_value.reference_price = 1
        proposal.return_value.maximum_risk = 0
        run_shadow_cycle()
        self.assertEqual(send.call_count, 1)
