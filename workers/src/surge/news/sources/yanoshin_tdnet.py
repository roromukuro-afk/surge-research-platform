"""Yanoshin TDnet WebAPI: a discovery source for Japanese timely disclosure.

TDnet itself cannot be collected - its robots.txt disallows the whole host and
its pages prohibit reproduction. This service publishes an *index* of the same
disclosures, and the operator frames it that way: index information plus links,
with the reader directed to the originating site for the documents.

So this module takes the index and stops there. It records what was disclosed,
by whom, when, and where the document lives; it never fetches the document. That
is not caution, it is the licence: ``news.source_policies`` marks full-text
storage PROHIBITED for this source, so a body write is refused in the database
as well as declined here.

**The documented cursor does not work.** ``llms.txt`` describes ``since_id`` as
"Return items with ID greater than this". It is not implemented that way - the
value is parsed as a Unix timestamp, so ``since_id=1281122`` becomes "period
starting 1970-01-16" and filters nothing, and ``by_id`` becomes "period ending
1970-01-16" and returns zero rows. A collector built on the documentation would
re-read the same window forever, or read nothing and look like a quiet day.
Measured 2026-09-17; see ``docs/research/yanoshin-tdnet-api.md``.

The cursor is therefore a client-side high-water mark on the item id, which is
what it was always meant to be. Ids are assigned in insert order, so anything
new carries a higher id than anything we have seen - even though an item can
carry a higher id and an *earlier* pubdate, which is why the id is a watermark
and never a sort key for time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from surge.http_fetch import DEFAULT_USER_AGENT, fetch
from surge.news.collector import SourceSpec
from surge.news.feeds import JST
from surge.news.models import AccessMechanism, AuthRequirement, DocumentType, FeedItem, SourceKind, TimePrecision

SOURCE_KEY = "yanoshin_tdnet"
BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"
ADAPTER_VERSION = "yanoshin-tdnet-1.0.0"

#: Politeness, not compliance. The service publishes no rate limit, so this is a
#: configurable courtesy rather than a documented figure to obey. Inventing a
#: number and calling it "the rate limit" would be worse than admitting there is
#: none.
DEFAULT_PAUSE_SECONDS = 0.3

#: The service returned 300 items spanning 2.3 days when measured, at roughly
#: 130 disclosures a day. A daily poll of the default page therefore has about a
#: day of headroom; a poll that falls further behind than that must recover
#: through the date range rather than through this page.
DEFAULT_LIMIT = 300
MEASURED_ITEMS_PER_DAY = 130


class CodeNormalisation(StrEnum):
    FIVE_CHAR_TRAILING_ZERO_STRIPPED = "FIVE_CHAR_TRAILING_ZERO_STRIPPED"
    FIVE_CHAR_KEPT = "FIVE_CHAR_KEPT"
    ALREADY_FOUR_CHAR = "ALREADY_FOUR_CHAR"
    UNEXPECTED_SHAPE = "UNEXPECTED_SHAPE"


_FOUR_CHAR = re.compile(r"^[0-9A-Z]{4}$")
_FIVE_CHAR = re.compile(r"^[0-9A-Z]{5}$")


@dataclass(frozen=True)
class NormalisedCode:
    raw: str
    normalised: str
    kind: CodeNormalisation

    @property
    def is_resolvable(self) -> bool:
        return self.kind is not CodeNormalisation.UNEXPECTED_SHAPE


def normalise_company_code(raw: str | None) -> NormalisedCode:
    """Turn a five-character TDnet code into something the master can match.

    The rule, and the reason it is not "strip the trailing zero":

    * ``72030`` -> ``7203``  and ``130A0`` -> ``130A``. A fifth character of
      ``0`` is the ordinary-share suffix and carries no information.
    * ``587A4`` and ``13264`` keep **all five characters**. A non-zero fifth
      character marks a non-ordinary security - an ETF, an ETN, a preferred
      class - and stripping it would merge that security with a different
      issuer entirely. Both of those appeared on the first page of live data,
      so this is not a hypothetical.

    Anything else is returned unchanged as ``UNEXPECTED_SHAPE`` rather than
    guessed at, and the raw value always survives alongside the result.
    """

    text = (raw or "").strip().upper()
    if not text:
        return NormalisedCode(raw=raw or "", normalised="", kind=CodeNormalisation.UNEXPECTED_SHAPE)

    if _FOUR_CHAR.match(text):
        return NormalisedCode(raw=text, normalised=text, kind=CodeNormalisation.ALREADY_FOUR_CHAR)

    if _FIVE_CHAR.match(text):
        if text[4] == "0":
            return NormalisedCode(
                raw=text,
                normalised=text[:4],
                kind=CodeNormalisation.FIVE_CHAR_TRAILING_ZERO_STRIPPED,
            )
        return NormalisedCode(raw=text, normalised=text, kind=CodeNormalisation.FIVE_CHAR_KEPT)

    return NormalisedCode(raw=text, normalised=text, kind=CodeNormalisation.UNEXPECTED_SHAPE)


@dataclass(frozen=True)
class TdnetItem:
    """One index row, exactly as the service gave it plus what we derived."""

    yanoshin_id: int
    pubdate: datetime
    code: NormalisedCode
    title: str
    company_name: str | None = None
    document_url: str | None = None
    url_xbrl: str | None = None
    markets_string: str | None = None
    update_history: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def tdnet_document_url(self) -> str | None:
        """The real TDnet URL, unwrapped from the service's redirect.

        ``document_url`` arrives as ``https://webapi.yanoshin.jp/rd.php?<real url>``.
        Unwrapping it locally means a later reader can see which TDnet file the
        row refers to without anyone having to follow a redirect - and following
        it is exactly what this source's policy does not permit.
        """

        if not self.document_url:
            return None
        marker = "rd.php?"
        index = self.document_url.find(marker)
        if index == -1:
            return self.document_url
        return self.document_url[index + len(marker) :] or None

    def as_feed_item(self) -> FeedItem:
        return FeedItem(
            source_document_id=str(self.yanoshin_id),
            title=self.title,
            url=self.tdnet_document_url,
            published_at=self.pubdate,
            published_precision=TimePrecision.EXACT,
            # No summary: the service gives a title and a link, and the body is
            # at TDnet where we may not take it.
            summary=None,
            language="ja",
            raw_fields={"raw_company_code": self.code.raw},
        )


SPEC = SourceSpec(
    source_key=SOURCE_KEY,
    name="Yanoshin TDnet WebAPI（非公式・Discovery）",
    scope="JP",
    source_kind=SourceKind.OFFICIAL_DISCLOSURE,
    access_mechanism=AccessMechanism.REST_API,
    official_url="https://webapi.yanoshin.jp/",
    feed_url=f"{BASE_URL}/recent.json",
    docs_url="https://webapi.yanoshin.jp/llms.txt",
    auth_requirement=AuthRequirement.NONE,
    documented_rate_limit="none published; llms.txt asks only that request frequency be reasonable",
    min_request_interval_seconds=DEFAULT_PAUSE_SECONDS,
    # Discovery only. An unofficial index of a source we cannot read directly
    # cannot also be the thing that confirms what a disclosure said.
    discovery_role=True,
    verification_role=False,
    fetch_priority=15,
    default_document_type=DocumentType.TIMELY_DISCLOSURE,
)


class YanoshinError(RuntimeError):
    pass


@dataclass
class FetchResult:
    items: list[TdnetItem]
    endpoint: str
    response_sha256: str
    fetched_at: datetime
    total_count: int | None = None
    condition_desc: str | None = None

    @property
    def newest_pubdate(self) -> datetime | None:
        return max((item.pubdate for item in self.items), default=None)

    @property
    def max_id(self) -> int | None:
        return max((item.yanoshin_id for item in self.items), default=None)

    def collection_lag_seconds(self) -> float | None:
        """How stale our view of the feed is, in seconds.

        Measured from the newest disclosure we retrieved to the moment we
        retrieved it. This is the number that says whether a polling interval is
        adequate; a row count never does.
        """

        newest = self.newest_pubdate
        if newest is None:
            return None
        return (self.fetched_at - newest).total_seconds()


@dataclass
class YanoshinTdnetSource:
    """The adapter. Fetches the index; never the documents behind it."""

    spec: SourceSpec = SPEC
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    limit: int = DEFAULT_LIMIT
    timeout: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT
    #: Client-side high-water mark. The server parameter that claims to do this
    #: does not, so the filtering happens here.
    last_seen_id: int | None = None
    #: Responses keyed by endpoint, so a repeated call inside one pass does not
    #: hit the service twice. Deliberately small and short-lived.
    _cache: dict[str, FetchResult] = field(default_factory=dict, repr=False)
    cache_ttl_seconds: float = 4 * 3600.0
    last_result: FetchResult | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- fetching

    def _get(self, condition: str, *, now: datetime | None = None, **params) -> FetchResult:
        query = {"limit": str(self.limit), **{k: str(v) for k, v in params.items() if v is not None}}
        # since_id and by_id are accepted by the service and do not mean what
        # their names say; sending them would filter by a date near the Unix
        # epoch. Refusing here stops a well-meaning caller reinstating them.
        forbidden = {"since_id", "by_id"} & set(query)
        if forbidden:
            raise YanoshinError(
                f"{sorted(forbidden)} are parsed by the service as Unix timestamps, not item ids. "
                "Use last_seen_id for the incremental cursor and a date range for recovery."
            )

        endpoint = f"{BASE_URL}/{condition}.json?" + "&".join(f"{k}={v}" for k, v in sorted(query.items()))
        cached = self._cache.get(endpoint)
        fetched_at = now or datetime.now(JST).astimezone()
        if cached is not None and (fetched_at - cached.fetched_at).total_seconds() < self.cache_ttl_seconds:
            return cached

        response = fetch(
            endpoint,
            timeout=self.timeout,
            user_agent=self.user_agent,
            min_interval=self.pause_seconds,
        )
        if response.status != 200:
            raise YanoshinError(f"{SOURCE_KEY}: HTTP {response.status} from {endpoint}")

        result = parse_response(response.body, endpoint=endpoint, fetched_at=fetched_at)
        self._cache[endpoint] = result
        self.last_result = result
        return result

    # ------------------------------------------------------------ the two modes

    def fetch_recent(self, *, now: datetime | None = None) -> FetchResult:
        """The incremental pass. Everything newer than the high-water mark."""

        result = self._get("recent", now=now)
        return self._apply_cursor(result)

    def fetch_date_range(self, start: date, end: date, *, now: datetime | None = None) -> FetchResult:
        """Backfill, gap recovery and integrity checking.

        The date range is the only filter the service actually implements
        correctly, so anything that needs to be sure of a window uses this and
        not the cursor.
        """

        if end < start:
            raise YanoshinError(f"end {end} is before start {start}")
        condition = f"{start:%Y%m%d}-{end:%Y%m%d}" if start != end else f"{start:%Y%m%d}"
        return self._get(condition, now=now)

    def fetch_for_codes(self, codes: Sequence[str], *, now: datetime | None = None) -> FetchResult:
        """One or more issuers. Codes are joined with hyphens by the service."""

        if not codes:
            raise YanoshinError("no codes given")
        return self._get("-".join(code.strip().upper() for code in codes), now=now)

    def _apply_cursor(self, result: FetchResult) -> FetchResult:
        if self.last_seen_id is None:
            return result
        kept = [item for item in result.items if item.yanoshin_id > self.last_seen_id]
        return FetchResult(
            items=kept,
            endpoint=result.endpoint,
            response_sha256=result.response_sha256,
            fetched_at=result.fetched_at,
            total_count=result.total_count,
            condition_desc=result.condition_desc,
        )

    def advance_cursor(self, result: FetchResult) -> int | None:
        """Move the high-water mark, never backwards."""

        highest = result.max_id
        if highest is None:
            return self.last_seen_id
        if self.last_seen_id is None or highest > self.last_seen_id:
            self.last_seen_id = highest
        return self.last_seen_id

    def window_is_safe(self, result: FetchResult) -> bool:
        """Whether ``recent`` could plausibly have covered everything missed.

        If the page came back full, it may have been truncated and something
        older than its oldest row could have been dropped. Saying so lets the
        caller fall back to a date range instead of assuming the gap is closed.
        """

        return len(result.items) < self.limit

    # --------------------------------------------------------- collector shape

    def list_items(self, *, since: datetime | None = None) -> Sequence[FeedItem]:
        result = self.fetch_recent()
        items = result.items
        if since is not None:
            items = [item for item in items if item.pubdate >= since]
        return [item.as_feed_item() for item in items]

    def fetch_body(self, item: FeedItem) -> str | None:
        """Always None, and that is the point.

        The body is a TDnet PDF. Reaching an index that links to it does not
        license storing it, so this adapter does not go and get it. The database
        refuses a full-text write for this source as well, which is the belt to
        this brace.
        """

        return None


# ------------------------------------------------------------------- parsing


def parse_response(payload: bytes, *, endpoint: str, fetched_at: datetime) -> FetchResult:
    """Parse one JSON response into items.

    The nesting key is ``Tdnet``. The published ``llms.txt`` says ``TDnet``, and
    a parser that trusted it would return zero items on every call - which looks
    exactly like a day on which nothing was disclosed. Both spellings are
    accepted so a future correction upstream does not break us either way.
    """

    digest = hashlib.sha256(payload).hexdigest()
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YanoshinError(f"response from {endpoint} is not JSON: {exc}") from exc

    items: list[TdnetItem] = []
    for entry in document.get("items", []):
        row = entry.get("Tdnet") or entry.get("TDnet") or entry
        raw_id = str(row.get("id", "")).strip()
        if not raw_id.isdigit():
            continue
        pubdate = _parse_pubdate(row.get("pubdate"))
        if pubdate is None:
            continue
        items.append(
            TdnetItem(
                yanoshin_id=int(raw_id),
                pubdate=pubdate,
                code=normalise_company_code(row.get("company_code")),
                title=str(row.get("title") or "").strip(),
                company_name=row.get("company_name"),
                document_url=row.get("document_url"),
                url_xbrl=row.get("url_xbrl"),
                markets_string=row.get("markets_string"),
                update_history=row.get("update_history"),
                raw=row,
            )
        )

    return FetchResult(
        items=items,
        endpoint=endpoint,
        response_sha256=digest,
        fetched_at=fetched_at,
        total_count=document.get("total_count"),
        condition_desc=document.get("condition_desc"),
    )


def _parse_pubdate(value) -> datetime | None:
    """``"2026-09-17 13:00:00"`` in JST, with no offset of its own.

    The service stamps Tokyo time and says so nowhere in the payload. Reading it
    as UTC would move every disclosure nine hours earlier - across the close, in
    most cases - so the zone is applied here explicitly rather than inferred.
    """

    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=JST)
    return None


def recovery_window(last_success: datetime | None, now: datetime, *, max_days: int = 14) -> tuple[date, date]:
    """The date range to re-read when the cursor cannot be trusted.

    Deliberately generous at the start and bounded at the end: re-reading a day
    we already have costs one request and produces duplicates the collector
    already knows how to drop, while missing a day produces a silent hole.
    """

    end = now.astimezone(JST).date()
    if last_success is None:
        return end - timedelta(days=1), end
    start = last_success.astimezone(JST).date() - timedelta(days=1)
    earliest = end - timedelta(days=max_days)
    return max(start, earliest), end
