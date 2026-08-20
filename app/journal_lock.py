from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import TextIO


LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.01


def acquire_file_lock(
    handle: TextIO,
    operation: int,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> None:
    """Acquire a cooperative file lock with a fail-closed time bound."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise TimeoutError("Journal lock acquisition timed out.") from error
            time.sleep(LOCK_RETRY_SECONDS)


def ensure_durable_parent(path: Path) -> None:
    """Create a missing journal parent and durably record the directory entry."""

    parent = path.parent
    existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if existed:
        return
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    ancestor_descriptor = os.open(
        parent.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(ancestor_descriptor)
    finally:
        os.close(ancestor_descriptor)


def fsync_containing_directory(path: Path) -> None:
    """Fsync a journal's containing directory."""

    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def establish_file_durability(handle: TextIO, path: Path) -> None:
    """Flush and fsync a journal, then fsync its containing directory."""

    handle.flush()
    os.fsync(handle.fileno())
    fsync_containing_directory(path)
