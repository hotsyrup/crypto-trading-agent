import os
import unittest
from unittest.mock import patch

from app.shadow_monitor import validate_execution_boundary


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

