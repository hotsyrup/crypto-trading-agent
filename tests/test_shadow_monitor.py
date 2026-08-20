import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.paper_cycle_ledger import LedgerConflictError, read_ledger
from app.paper_execution import PaperOrder
from app.paper_portfolio import PaperPortfolio, load_portfolio
from app.risk_accounting import (
    RiskAccountingDecision,
    RiskAccountingTransaction,
    portfolio_risk_transaction,
)
from app.safety_gate import SafetyGateDecision
from app.shadow_monitor import (
    HealthHandler,
    STATE,
    TimedHTTPServer,
    _record_cycle_failure,
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

    @staticmethod
    @contextmanager
    def risk_transaction(accounting, commit):
        transaction = MagicMock()
        transaction.decision = accounting
        transaction.commit_cycle = commit
        yield transaction

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
        commit = MagicMock(
            return_value=self.ledger_result(
                eligible=True,
                simulated=True,
                status="SIMULATED",
            )
        )
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch(
                "app.shadow_monitor.portfolio_risk_transaction",
                return_value=self.risk_transaction(accounting, commit),
            ),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=True),
            patch("app.shadow_monitor.simulate_order", return_value=order),
            patch("app.shadow_monitor.apply_order", return_value=updated),
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
        commit = MagicMock(
            return_value=self.ledger_result(eligible=False, simulated=False)
        )
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch(
                "app.shadow_monitor.portfolio_risk_transaction",
                return_value=self.risk_transaction(accounting, commit),
            ),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=False),
            patch("app.shadow_monitor.simulate_order") as simulate,
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
        commit = MagicMock(
            return_value=self.ledger_result(
                eligible=False,
                simulated=False,
                duplicate=True,
            )
        )
        with (
            patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
            patch("app.shadow_monitor.load_research_evidence", return_value=research),
            patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
            patch("app.shadow_monitor.load_portfolio", return_value=portfolio),
            patch(
                "app.shadow_monitor.portfolio_risk_transaction",
                return_value=self.risk_transaction(accounting, commit),
            ),
            patch("app.shadow_monitor.acceptance_credit_enabled", return_value=False),
            patch("app.shadow_monitor.record_decision") as record,
            patch("app.shadow_monitor._write_operator_status"),
            patch("app.shadow_monitor.report_is_due", return_value=False),
        ):
            run_shadow_cycle()
        record.assert_not_called()
        self.assertTrue(STATE["paper_cycle_duplicate"])

    def test_production_retry_after_simulated_trade_returns_original_cycle(self):
        proposal, research, safety, _ = self.common()
        loaded_portfolios = []
        transaction_times = []

        def observed_load():
            portfolio = load_portfolio()
            loaded_portfolios.append(portfolio)
            return portfolio

        def observed_transaction(*args, **kwargs):
            transaction_times.append(kwargs["now"])
            return portfolio_risk_transaction(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            ledger_path = directory_path / "ledger.jsonl"
            risk_path = directory_path / "risk.json"
            with (
                patch("app.paper_cycle_ledger.LEDGER_PATH", ledger_path),
                patch(
                    "app.paper_cycle_ledger.LOCK_PATH",
                    directory_path / "ledger.lock",
                ),
                patch("app.risk_accounting.RISK_STATE_PATH", risk_path),
                patch(
                    "app.risk_accounting.RISK_LOCK_PATH",
                    directory_path / "risk.lock",
                ),
                patch("app.shadow_monitor.create_trade_proposal", return_value=proposal),
                patch(
                    "app.shadow_monitor.load_research_evidence",
                    return_value=research,
                ) as research_loader,
                patch("app.shadow_monitor.evaluate_safety_gate", return_value=safety),
                patch(
                    "app.shadow_monitor.load_portfolio",
                    side_effect=observed_load,
                ),
                patch(
                    "app.shadow_monitor.portfolio_risk_transaction",
                    side_effect=observed_transaction,
                ),
                patch("app.shadow_monitor.acceptance_credit_enabled", return_value=True),
                patch("app.shadow_monitor.record_decision") as record,
                patch("app.shadow_monitor._write_operator_status"),
                patch("app.shadow_monitor.report_is_due", return_value=False),
                patch("builtins.print"),
            ):
                run_shadow_cycle()
                original = read_ledger()[0]
                cache_after_first = risk_path.read_bytes()
                run_shadow_cycle()
                records = read_ledger()
                cache_after_retry = risk_path.read_bytes()
                portfolios_after_retry = list(loaded_portfolios)
                research_loader.return_value = SimpleNamespace(
                    **{**research.__dict__, "reason": "Changed research evidence."}
                )
                with self.assertRaisesRegex(
                    LedgerConflictError, "immutable signal evidence"
                ):
                    run_shadow_cycle()
                self.assertEqual(len(read_ledger()), 1)
                self.assertEqual(risk_path.read_bytes(), cache_after_retry)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["entry_hash"], original["entry_hash"])
        self.assertEqual(cache_after_retry, cache_after_first)
        self.assertEqual(len(portfolios_after_retry), 2)
        self.assertGreater(transaction_times[1], transaction_times[0])
        self.assertEqual(
            portfolios_after_retry[1],
            PaperPortfolio(
                Decimal(original["portfolio_after"]["usdc_balance"]),
                Decimal(original["portfolio_after"]["eth_balance"]),
            ),
        )
        self.assertEqual(
            records[0]["portfolio_after"],
            STATE["paper_portfolio_after"],
        )
        self.assertTrue(STATE["paper_cycle_duplicate"])
        self.assertEqual(STATE["ledger_head"], original["entry_hash"])
        self.assertEqual(record.call_count, 1)

    def test_stale_concurrent_cycle_cannot_commit_its_risk_update(self):
        proposal_a, research, safety, _ = self.common()
        proposal_a = SimpleNamespace(
            **{
                **proposal_a.__dict__,
                "signal": Signal.SELL,
                "reference_price": Decimal("2200"),
                "maximum_risk": Decimal("500"),
            }
        )
        proposal_b = SimpleNamespace(
            **{
                **proposal_a.__dict__,
                "reference_price": Decimal("1800"),
                "market_data_observed_at": self.observed_at.replace(second=1),
                "market_data_received_at": self.observed_at.replace(second=1),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            ledger_path = directory_path / "ledger.jsonl"
            risk_path = directory_path / "risk.json"
            ledger_lock_path = directory_path / "ledger.lock"
            risk_lock_path = directory_path / "risk.lock"
            with (
                patch("app.paper_cycle_ledger.LEDGER_PATH", ledger_path),
                patch("app.paper_cycle_ledger.LOCK_PATH", ledger_lock_path),
                patch("app.risk_accounting.RISK_STATE_PATH", risk_path),
                patch("app.risk_accounting.RISK_LOCK_PATH", risk_lock_path),
            ):
                seed_id = hashlib.sha256(b"seed-eth-position").hexdigest()
                with portfolio_risk_transaction(
                    PaperPortfolio(Decimal("10000.00"), Decimal("0")),
                    Decimal("2000"),
                    now=self.observed_at,
                ) as seed_transaction:
                    seed_decision = seed_transaction.decision
                    seed_transaction.commit_cycle({
                        "recorded_at": self.observed_at.isoformat(),
                        "cycle_id": seed_id,
                        "signal_id": seed_id,
                        "reference_price": "2000",
                        "accounting_date": self.observed_at.date().isoformat(),
                        "portfolio_value": str(seed_decision.current_value),
                        "high_water_mark": str(seed_decision.high_water_mark),
                        "daily_start_value": str(seed_decision.daily_start_value),
                        "system_healthy": True,
                        "paper_eligible": False,
                        "simulated": True,
                        "blocked_reason": "seed",
                        "order": {"status": "SIMULATED"},
                        "portfolio_before": {
                            "usdc_balance": "10000.00",
                            "eth_balance": "0",
                        },
                        "portfolio_after": {
                            "usdc_balance": "9000",
                            "eth_balance": "0.5",
                        },
                    })

                both_loaded = threading.Barrier(2)
                a_committed = threading.Event()
                loaded_portfolios = {}
                errors = {}
                actual_transaction_commit = RiskAccountingTransaction.commit_cycle

                def concurrent_load():
                    name = threading.current_thread().name
                    portfolio = load_portfolio()
                    loaded_portfolios[name] = portfolio
                    both_loaded.wait(timeout=2)
                    if name == "cycle-b" and not a_committed.wait(timeout=2):
                        raise AssertionError("cycle A did not commit")
                    return portfolio

                def concurrent_proposal():
                    if threading.current_thread().name == "cycle-a":
                        return proposal_a
                    return proposal_b

                def observed_commit(transaction, payload):
                    result = actual_transaction_commit(transaction, payload)
                    if threading.current_thread().name == "cycle-a":
                        a_committed.set()
                    return result

                def run_cycle():
                    name = threading.current_thread().name
                    try:
                        run_shadow_cycle()
                    except Exception as error:
                        errors[name] = error

                with (
                    patch(
                        "app.shadow_monitor.create_trade_proposal",
                        side_effect=concurrent_proposal,
                    ),
                    patch(
                        "app.shadow_monitor.load_research_evidence",
                        return_value=research,
                    ),
                    patch(
                        "app.shadow_monitor.evaluate_safety_gate",
                        return_value=safety,
                    ),
                    patch(
                        "app.shadow_monitor.load_portfolio",
                        side_effect=concurrent_load,
                    ),
                    patch(
                        "app.risk_accounting.RiskAccountingTransaction.commit_cycle",
                        autospec=True,
                        side_effect=observed_commit,
                    ),
                    patch(
                        "app.shadow_monitor.acceptance_credit_enabled",
                        return_value=True,
                    ),
                    patch("app.shadow_monitor.record_decision"),
                    patch("app.shadow_monitor._write_operator_status"),
                    patch("app.shadow_monitor.report_is_due", return_value=False),
                    patch("builtins.print"),
                ):
                    cycle_a = threading.Thread(target=run_cycle, name="cycle-a")
                    cycle_b = threading.Thread(target=run_cycle, name="cycle-b")
                    cycle_a.start()
                    cycle_b.start()
                    cycle_a.join(timeout=3)
                    cycle_b.join(timeout=3)

                self.assertFalse(cycle_a.is_alive())
                self.assertFalse(cycle_b.is_alive())
                self.assertEqual(loaded_portfolios["cycle-a"], loaded_portfolios["cycle-b"])
                self.assertNotIn("cycle-a", errors)
                self.assertIsInstance(errors.get("cycle-b"), LedgerConflictError)

                records = read_ledger()
                self.assertEqual(len(records), 2)
                accepted_cycle = records[-1]
                self.assertEqual(accepted_cycle["reference_price"], "2200")
                risk_state = json.loads(risk_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    Decimal(risk_state["last_portfolio_value"]),
                    Decimal(accepted_cycle["portfolio_value"]),
                )
                self.assertEqual(
                    Decimal(risk_state["last_portfolio_value"]),
                    Decimal("10100.0"),
                )

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

    def test_cycle_failure_replaces_stale_healthy_operator_status(self):
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        STATE.update(
            mode="monitoring_only",
            status="healthy",
            last_cycle_at="2026-08-10T17:00:00+00:00",
            last_error=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "operator_status.json"
            status_path.write_text(
                json.dumps({"state": {"status": "healthy"}}),
                encoding="utf-8",
            )
            with (
                patch("app.shadow_monitor.OPERATOR_STATUS_PATH", status_path),
                patch("app.shadow_monitor.ledger_status") as ledger,
                patch("app.shadow_monitor.legacy_progress_status") as legacy,
                patch("builtins.print"),
            ):
                _record_cycle_failure(RuntimeError("cycle failed"))

            report = json.loads(status_path.read_text(encoding="utf-8"))

        ledger.assert_not_called()
        legacy.assert_not_called()
        self.assertEqual(report["report_status"], "failure_fallback")
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["live_route"])
        self.assertEqual(report["signing_authority"], "none")
        self.assertEqual(report["state"]["status"], "failed")
        self.assertEqual(report["state"]["last_error"], "RuntimeError")
        self.assertEqual(report["ledger"], {"status": "unavailable"})

    def test_cycle_failure_survives_operator_status_write_failure(self):
        original_state = STATE.copy()
        self.addCleanup(lambda: (STATE.clear(), STATE.update(original_state)))
        with (
            patch(
                "app.shadow_monitor._write_failure_operator_status",
                side_effect=OSError("status path unavailable"),
            ),
            patch("builtins.print") as output,
        ):
            _record_cycle_failure(ValueError("cycle failed"))

        self.assertEqual(STATE["status"], "failed")
        self.assertEqual(STATE["last_error"], "ValueError")
        self.assertEqual(STATE["operator_status_write_error"], "OSError")
        output.assert_called_once()

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
