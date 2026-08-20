import tempfile
import unittest
from pathlib import Path

from app.economic_value_ledger import append_event, read_ledger


class EconomicValueLedgerTests(unittest.TestCase):
    def paths(self, directory):
        return Path(directory) / "economic.jsonl", Path(directory) / "economic.lock"

    def test_request_result_and_use_form_a_verified_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            path, lock = self.paths(directory)
            append_event("request_recorded", {
                "request_id": "req-1", "requested_by_agent": "research-agent",
                "provider": "dexscreener", "purpose": "Base market observation",
                "cost": "0", "cost_currency": "USD", "cost_status": "known",
            }, event_id="evt-1", recorded_at="2026-08-18T12:00:00+00:00", path=path, lock_path=lock)
            append_event("result_recorded", {
                "request_id": "req-1", "result_id": "packet-1", "output_type": "research_packet",
                "output_hash": "a" * 64, "outcome": "success",
            }, event_id="evt-2", recorded_at="2026-08-18T12:00:01+00:00", path=path, lock_path=lock)
            append_event("usage_recorded", {
                "request_id": "req-1", "result_id": "packet-1", "used_by_agent": "trading-agent",
                "use_type": "input_to_decision", "use_reference": "paper-cycle-7",
            }, event_id="evt-3", recorded_at="2026-08-18T12:00:02+00:00", path=path, lock_path=lock)
            events = read_ledger(path)
        self.assertEqual([event["event_type"] for event in events], [
            "request_recorded", "result_recorded", "usage_recorded",
        ])

    def test_unknown_cost_must_be_null(self):
        with tempfile.TemporaryDirectory() as directory:
            path, lock = self.paths(directory)
            with self.assertRaisesRegex(ValueError, "Unknown cost"):
                append_event("request_recorded", {
                    "request_id": "req-1", "requested_by_agent": "research-agent",
                    "provider": "provider", "purpose": "research", "cost": "1",
                    "cost_currency": "USD", "cost_status": "unknown",
                }, event_id="evt-1", path=path, lock_path=lock)

    def test_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, lock = self.paths(directory)
            append_event("request_recorded", {
                "request_id": "req-1", "requested_by_agent": "research-agent",
                "provider": "dexscreener", "purpose": "research", "cost": "0",
                "cost_currency": "USD", "cost_status": "known",
            }, event_id="evt-1", path=path, lock_path=lock)
            path.write_text(path.read_text().replace('"cost":"0"', '"cost":"9"'))
            with self.assertRaisesRegex(ValueError, "hash chain"):
                read_ledger(path)


if __name__ == "__main__":
    unittest.main()
