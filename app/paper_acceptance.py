"""Paper acceptance policy switches and legacy-progress boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


LEGACY_ACCEPTANCE_PATH = Path("data/paper_acceptance.json")


def acceptance_credit_enabled() -> bool:
    """Credit stays frozen until Ben separately approves the corrected run."""
    return os.getenv("PAPER_ACCEPTANCE_CREDIT_ENABLED", "false").strip().lower() == "true"


def legacy_progress_status() -> dict[str, object]:
    status: dict[str, object] = {
        "path": str(LEGACY_ACCEPTANCE_PATH),
        "present": LEGACY_ACCEPTANCE_PATH.exists(),
        "read_for_credit": False,
        "status": "INVALIDATED_PRE_FIX_EVIDENCE",
    }
    if not LEGACY_ACCEPTANCE_PATH.exists():
        status["sha256"] = None
        return status
    try:
        raw = LEGACY_ACCEPTANCE_PATH.read_bytes()
        status["sha256"] = hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw)
        if isinstance(payload, dict):
            status["reported_cycles"] = payload.get("cycles")
            status["reported_eligible_cycles"] = payload.get("eligible_cycles")
            status["reported_simulated_orders"] = payload.get("simulated_orders")
    except (OSError, json.JSONDecodeError) as error:
        status["sha256"] = None
        status["evidence_error_type"] = type(error).__name__
    return status
