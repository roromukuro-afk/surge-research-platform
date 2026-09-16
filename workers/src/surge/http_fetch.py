"""Minimal HTTP fetching with provenance.

Every fetch records when it was requested, when it arrived, the HTTP status, the
byte count and a content hash, so a later run can prove what it was looking at.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_USER_AGENT = "surge-research-platform/0.1 (contact configured via SURGE_CONTACT_EMAIL)"


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    body: bytes
    requested_at: datetime
    received_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def bytes(self) -> int:
        return len(self.body)


def fetch(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 60.0,
    retries: int = 3,
    min_interval: float = 0.0,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    """GET a URL, retrying transient failures.

    ``min_interval`` lets callers respect a source's published request rate
    (for example SEC's 10 requests/second limit).
    """

    # No Accept-Encoding: urllib does not decompress, and a silently gzipped body
    # would look like corrupt data rather than a transport detail.
    request_headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    request_headers.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(retries):
        requested_at = datetime.now(UTC)
        try:
            request = urllib.request.Request(url, headers=request_headers)  # noqa: S310 - fixed https sources
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
                received_at = datetime.now(UTC)
                if min_interval:
                    time.sleep(min_interval)
                return HttpResponse(
                    url=url,
                    status=response.status,
                    body=body,
                    requested_at=requested_at,
                    received_at=received_at,
                )
        except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover - network dependent
            last_error = exc
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"fetch failed after {retries} attempts: {url}: {last_error}")
