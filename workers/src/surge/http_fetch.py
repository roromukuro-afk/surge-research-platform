"""Minimal HTTP fetching with provenance.

Every fetch records when it was requested, when it arrived, the HTTP status, the
byte count and a content hash, so a later run can prove what it was looking at.
"""

from __future__ import annotations

import email.utils
import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_USER_AGENT = "surge-research-platform/0.1 (contact configured via SURGE_CONTACT_EMAIL)"


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    body: bytes
    requested_at: datetime
    received_at: datetime
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def bytes(self) -> int:
        return len(self.body)

    def header(self, name: str) -> str | None:
        """Headers are case insensitive; callers should not have to remember that."""

        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    @property
    def last_modified(self) -> datetime | None:
        """When the source says it published this, if it says so at all.

        The ECB data API returns Last-Modified at the moment the reference rates
        went out, which is a real publication time rather than our own receipt
        time - the difference is exactly what ``availability_basis`` records.
        """

        raw = self.header("Last-Modified")
        if not raw:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fetch(
    url: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 60.0,
    retries: int = 3,
    min_interval: float = 0.0,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
    accept_statuses: tuple[int, ...] = (),
) -> HttpResponse:
    """Make a request, retrying transient failures.

    ``min_interval`` lets callers respect a source's published request rate
    (for example SEC's 10 requests/second limit). ``accept_statuses`` names
    error statuses that are an answer rather than a failure - a 304 from a
    conditional refetch, or a 412 from a write-once PUT that found the object
    already there.
    """

    # No Accept-Encoding: urllib does not decompress, and a silently gzipped body
    # would look like corrupt data rather than a transport detail.
    request_headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    request_headers.update(headers or {})

    last_error: Exception | None = None
    for attempt in range(retries):
        requested_at = datetime.now(UTC)
        try:
            request = urllib.request.Request(  # noqa: S310 - fixed https sources
                url, headers=request_headers, data=data, method=method
            )
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
                    headers=dict(response.headers),
                )
        except urllib.error.HTTPError as exc:
            if exc.code in accept_statuses:
                return HttpResponse(
                    url=url,
                    status=exc.code,
                    body=exc.read(),
                    requested_at=requested_at,
                    received_at=datetime.now(UTC),
                    headers=dict(exc.headers or {}),
                )
            # 4xx will not fix itself; only retry what might.
            if exc.code < 500 and exc.code != 429:
                raise
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover - network dependent
            last_error = exc
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"fetch failed after {retries} attempts: {url}: {last_error}")
