"""Bounded HTTPS transport adapter for observation-only research payloads."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_RESEARCH_URL = (
    "https://lumen-base-research-agent-production.up.railway.app"
    "/research/crypto/base/latest"
)
DEFAULT_RESEARCH_HOST = "lumen-base-research-agent-production.up.railway.app"
MAX_RESPONSE_BYTES = 2_000_000
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class ResearchPayloadAdapter(Protocol):
    def fetch(self) -> object:
        ...


@dataclass(frozen=True)
class HttpsResearchAdapter:
    url: str
    allowed_hosts: tuple[str, ...]
    timeout_seconds: float = 10
    max_attempts: int = 3
    opener: Callable[..., object] = field(
        default=urlopen,
        repr=False,
        compare=False,
    )
    sleeper: Callable[[float], None] = field(
        default=time.sleep,
        repr=False,
        compare=False,
    )

    def _request(self) -> Request:
        parsed = urlparse(self.url)
        hosts = {host.strip().lower() for host in self.allowed_hosts if host.strip()}
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in hosts
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Research feed must use an approved HTTPS host.")
        if not parsed.path.startswith("/research/") or parsed.query or parsed.fragment:
            raise ValueError("Research feed must use a query-free /research/ path.")
        if not self.timeout_seconds > 0:
            raise ValueError("Research transport timeout must be positive.")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("Research transport attempts must be between 1 and 3.")
        return Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "lumen-trader/3"},
            method="GET",
        )

    def fetch(self) -> object:
        request = self._request()
        for attempt in range(1, self.max_attempts + 1):
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Research response exceeds the size limit.")
                return json.loads(raw)
            except HTTPError as error:
                if (
                    error.code not in RETRYABLE_HTTP_STATUSES
                    or attempt == self.max_attempts
                ):
                    raise
            except (TimeoutError, URLError, OSError):
                if attempt == self.max_attempts:
                    raise
            self.sleeper(min(2 ** (attempt - 1), 2))
        raise RuntimeError("Research transport exhausted without a result.")


def configured_research_adapter() -> HttpsResearchAdapter:
    url = os.getenv("RESEARCH_FEED_URL", DEFAULT_RESEARCH_URL).strip()
    configured_hosts = os.getenv(
        "RESEARCH_FEED_ALLOWED_HOSTS",
        DEFAULT_RESEARCH_HOST,
    )
    hosts = tuple(
        host.strip().lower()
        for host in configured_hosts.split(",")
        if host.strip()
    )
    if not hosts:
        raise ValueError("RESEARCH_FEED_ALLOWED_HOSTS must name at least one host.")
    return HttpsResearchAdapter(url=url, allowed_hosts=hosts)
