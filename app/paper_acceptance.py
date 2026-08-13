"""Paper acceptance policy switches and legacy-progress boundary."""

from __future__ import annotations

import os
from pathlib import Path


LEGACY_ACCEPTANCE_PATH = Path("data/paper_acceptance.json")


def acceptance_credit_enabled() -> bool:
    """Credit stays frozen until Ben separately approves the corrected run."""
    return os.getenv("PAPER_ACCEPTANCE_CREDIT_ENABLED", "false").strip().lower() == "true"


def legacy_progress_status() -> dict[str, object]:
    return {
        "path": str(LEGACY_ACCEPTANCE_PATH),
        "present": LEGACY_ACCEPTANCE_PATH.exists(),
        "read_for_credit": False,
        "status": "INVALIDATED_PRE_FIX_EVIDENCE",
    }
