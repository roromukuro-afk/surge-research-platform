"""One adapter for every RSS and Atom source in the registry.

Most of the sources that survived the licence review publish RSS or Atom, so
they need one adapter rather than eight. What differs between them is data, not
code: the feed URL, the timezone to assume when a source stamps a bare local
time, and whether the licence lets us keep the body.

Conditional requests are the point of the ETag and Last-Modified handling. A
collector polling a central bank every few minutes should get a 304 almost every
time, and a source that has told us nothing changed is a source we should not
have downloaded again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from surge.http_fetch import DEFAULT_USER_AGENT, HttpResponse, fetch
from surge.news.collector import SourceSpec
from surge.news.feeds import JST, parse_feed
from surge.news.models import AccessMechanism, AuthRequirement, DocumentType, FeedItem, SourceKind


class NotModified(Exception):
    """The source says nothing has changed. Not an error - the desired outcome."""


@dataclass
class FeedSource:
    """An RSS or Atom source. Listing is a fetch; the body is in the listing.

    ``fetch_body`` deliberately returns the summary the feed itself carried
    rather than following the link. Fetching the article page would be a second
    request against a different licence surface - the terms we read covered the
    feed - and the summary is what the publisher chose to syndicate.
    """

    spec: SourceSpec
    assume_tz: timezone | None = None
    etag: str | None = None
    last_modified: str | None = None
    #: Set after each successful listing so a caller can persist the cursor.
    last_response: HttpResponse | None = field(default=None, repr=False)
    not_modified: bool = False
    timeout: float = 30.0
    #: Some sources refuse anonymous clients; SEC rejects a request whose
    #: User-Agent carries no contact address, so this is not decoration.
    user_agent: str = DEFAULT_USER_AGENT

    def list_items(self, *, since: datetime | None = None) -> Sequence[FeedItem]:
        if not self.spec.feed_url:
            raise ValueError(f"{self.spec.source_key} has no feed URL")

        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified

        response = fetch(
            self.spec.feed_url,
            headers=headers,
            timeout=self.timeout,
            user_agent=self.user_agent,
            min_interval=self.spec.min_request_interval_seconds,
            # 304 is the answer we want from a conditional refetch, not a failure.
            accept_statuses=(304,),
        )
        self.last_response = response
        self.not_modified = response.status == 304

        if self.not_modified:
            return []
        if response.status != 200:
            raise RuntimeError(f"{self.spec.source_key}: HTTP {response.status} from {self.spec.feed_url}")

        self.etag = response.header("ETag") or self.etag
        self.last_modified = response.header("Last-Modified") or self.last_modified

        items = parse_feed(response.body, assume_tz=self.assume_tz)
        if since is not None:
            # A published time we could not parse is kept, not dropped. Dropping
            # it would silently lose items from any source with a malformed date,
            # which is the failure mode hardest to notice.
            items = [
                item for item in items if item.published_at is None or item.published_at >= since
            ]
        return items

    def fetch_body(self, item: FeedItem) -> str | None:
        return item.summary


def _spec(
    key: str,
    name: str,
    scope: str,
    kind: SourceKind,
    url: str,
    feed: str,
    *,
    document_type: DocumentType,
    interval: float,
    mechanism: AccessMechanism = AccessMechanism.RSS,
    auth: AuthRequirement = AuthRequirement.NONE,
    required_env: tuple[str, ...] = (),
    verification: bool = False,
    priority: int = 100,
    rate_limit: str | None = None,
) -> SourceSpec:
    return SourceSpec(
        source_key=key,
        name=name,
        scope=scope,
        source_kind=kind,
        access_mechanism=mechanism,
        official_url=url,
        feed_url=feed,
        auth_requirement=auth,
        required_env=required_env,
        documented_rate_limit=rate_limit,
        min_request_interval_seconds=interval,
        verification_role=verification,
        fetch_priority=priority,
        default_document_type=document_type,
    )


#: The feed sources that need no credential. Mirrors news.sources; an integration
#: test checks the two against each other so they cannot drift apart unnoticed.
FEED_SPECS: dict[str, SourceSpec] = {
    "boj": _spec(
        "boj",
        "日本銀行 公表資料",
        "JP",
        SourceKind.CENTRAL_BANK,
        "https://www.boj.or.jp/",
        "https://www.boj.or.jp/rss/whatsnew.xml",
        document_type=DocumentType.POLICY_STATEMENT,
        interval=2.0,
        verification=True,
        priority=40,
        rate_limit="no numeric limit; the statistics API manual asks that high-frequency access be avoided",
    ),
    "federal_reserve": _spec(
        "federal_reserve",
        "Federal Reserve Board press releases",
        "US",
        SourceKind.CENTRAL_BANK,
        "https://www.federalreserve.gov/",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        document_type=DocumentType.POLICY_STATEMENT,
        interval=2.0,
        verification=True,
        priority=40,
    ),
    "whitehouse": _spec(
        "whitehouse",
        "White House",
        "US",
        SourceKind.GOVERNMENT,
        "https://www.whitehouse.gov/",
        "https://www.whitehouse.gov/presidential-actions/feed/",
        document_type=DocumentType.POLICY_STATEMENT,
        interval=2.0,
        priority=70,
    ),
    "sec_edgar": _spec(
        "sec_edgar",
        "SEC EDGAR filings",
        "US",
        SourceKind.REGULATOR,
        "https://www.sec.gov/edgar/search/",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&owner=include&count=40&output=atom",
        document_type=DocumentType.STATUTORY_FILING,
        interval=0.15,
        mechanism=AccessMechanism.ATOM,
        # SEC refuses anonymous clients. The contact address is not optional
        # politeness: without it the request is rejected, so it belongs in
        # required_env and a run without it is SKIPPED rather than FAILED.
        required_env=("SURGE_CONTACT_EMAIL",),
        verification=True,
        priority=30,
        rate_limit="10 requests per second, stated in three official places",
    ),
    "cao_kantei_egov": _spec(
        "cao_kantei_egov",
        "内閣府 / 首相官邸 / e-Gov",
        "JP",
        SourceKind.GOVERNMENT,
        "https://www.cao.go.jp/",
        "https://www.esri.cao.go.jp/rss-jp.xml",
        document_type=DocumentType.STATISTIC_RELEASE,
        interval=2.0,
        priority=70,
    ),
}


#: Sources that may stamp a bare local time. Naming the zone here rather than
#: guessing one in the parser keeps the assumption visible: an unlabelled
#: Japanese timestamp read as UTC lands nine hours early, which is a whole
#: trading session.
ASSUMED_TZ: dict[str, timezone] = {
    "boj": JST,
    "cao_kantei_egov": JST,
}


def feed_source(source_key: str, **overrides) -> FeedSource:
    """Build the adapter for a registered feed source."""

    spec = FEED_SPECS.get(source_key)
    if spec is None:
        raise KeyError(f"{source_key} is not a registered feed source: {sorted(FEED_SPECS)}")
    overrides.setdefault("assume_tz", ASSUMED_TZ.get(source_key))
    return FeedSource(spec=spec, **overrides)
