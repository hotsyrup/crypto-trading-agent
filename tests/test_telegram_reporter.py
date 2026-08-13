import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app.telegram_reporter as reporter


class TelegramReporterTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(reporter.reporting_enabled())

    def test_report_is_due_after_local_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            with patch.object(reporter, "MARKER_PATH", marker), patch.dict(
                os.environ,
                {
                    "TELEGRAM_REPORTING_ENABLED": "true",
                    "REPORT_TIMEZONE": "America/Los_Angeles",
                    "REPORT_HOUR_LOCAL": "9",
                },
                clear=True,
            ):
                self.assertTrue(
                    reporter.report_is_due(datetime(2026, 8, 6, 17, tzinfo=timezone.utc))
                )

    def test_report_states_execution_is_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            report = reporter.format_daily_report(
                {"mode": "monitoring_only", "status": "healthy"},
                datetime(2026, 8, 6, 17, tzinfo=timezone.utc),
            )
        self.assertIn("Execution: disabled", report)
        self.assertIn("Mode: monitoring_only", report)
        self.assertIn("PAPER ONLY — NO SIGNER OR LIVE ROUTE", report)
        self.assertIn("Acceptance credit: False", report)
