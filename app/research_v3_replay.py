"""Offline transports for deterministic Stage 4 provider replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from app.openai_research_v3 import RESPONSES_URL
from app.research_v3_runtime import JsonResult


@dataclass(frozen=True)
class RecordedJsonClient:
    """Serve exact URLs from recorded JSON files without network access."""

    recordings: dict[str, Path] = field(repr=False)

    def get(
        self,
        url: str,
        *,
        allowed_host: str,
        headers: dict[str, str] | None = None,
    ) -> JsonResult:
        del headers
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != allowed_host:
            raise ValueError("Recorded request does not match its approved HTTPS host.")
        path = self.recordings.get(url)
        if path is None:
            raise ValueError("No exact recorded response exists for the requested URL.")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Recorded provider response is invalid.") from error
        return JsonResult(
            payload=payload,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )


class _RecordedResponse:
    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._raw[:limit]


def recorded_opener(path: Path):
    """Return a urllib-compatible Responses opener backed by one fixture."""

    def open_recording(request, timeout: float):
        del timeout
        if request.full_url != RESPONSES_URL or request.get_method() != "POST":
            raise ValueError("Recorded OpenAI transport accepts only the Responses endpoint.")
        try:
            raw = path.read_bytes()
            json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Recorded OpenAI response is invalid.") from error
        return _RecordedResponse(raw)

    return open_recording
