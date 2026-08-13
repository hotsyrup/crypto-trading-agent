"""Persistent counters for the frozen seven-day paper acceptance run."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ACCEPTANCE_PATH = Path("data/paper_acceptance.json")
TARGET_DAYS = 7
TARGET_SIGNALS = 50


def update_acceptance(*, eligible: bool, simulated: bool, blocked_reason: str) -> dict:
    now = datetime.now(timezone.utc)
    if ACCEPTANCE_PATH.exists():
        with ACCEPTANCE_PATH.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    else:
        state = {
            "schema_version": 1,
            "started_at": now.isoformat(),
            "ends_at": (now + timedelta(days=TARGET_DAYS)).isoformat(),
            "cycles": 0,
            "eligible_cycles": 0,
            "simulated_orders": 0,
            "blocked_cycles": 0,
        }
    state["cycles"] += 1
    state["eligible_cycles"] += int(eligible)
    state["simulated_orders"] += int(simulated)
    state["blocked_cycles"] += int(not eligible)
    state["last_cycle_at"] = now.isoformat()
    state["last_blocked_reason"] = blocked_reason if not eligible else None
    started = datetime.fromisoformat(state["started_at"])
    state["complete"] = (
        now >= started + timedelta(days=TARGET_DAYS)
        or state["eligible_cycles"] >= TARGET_SIGNALS
    )

    ACCEPTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ACCEPTANCE_PATH.parent,
            prefix=".paper_acceptance.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(ACCEPTANCE_PATH)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return state
