import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paper_acceptance import update_acceptance


class PaperAcceptanceTests(unittest.TestCase):
    def test_counters_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "acceptance.json"
            with patch("app.paper_acceptance.ACCEPTANCE_PATH", path):
                first = update_acceptance(eligible=False, simulated=False, blocked_reason="stale")
                second = update_acceptance(eligible=True, simulated=True, blocked_reason="")
        self.assertEqual(first["cycles"], 1)
        self.assertEqual(second["cycles"], 2)
        self.assertEqual(second["eligible_cycles"], 1)
        self.assertEqual(second["simulated_orders"], 1)


if __name__ == "__main__":
    unittest.main()
