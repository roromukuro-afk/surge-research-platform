"""Phase 4: the collection framework, and the leak it exists to prevent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from surge.licensing import LicenseMode, LicenseViolation, Permission
from surge.news import (
    BodyStorage,
    CollectedDocument,
    DocumentType,
    FeedError,
    FeedItem,
    KnowledgeTimes,
    KnownDocument,
    SourcePolicy,
    SourceSpec,
    TimeError,
    TimePrecision,
    collect,
    content_fingerprint,
    parse_datetime,
    parse_feed,
)
from surge.news.feeds import JST
from surge.news.models import AccessMechanism, AuthRequirement, SourceKind

PUBLISHED = datetime(2025, 3, 4, 6, 0, tzinfo=UTC)
SEEN = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
INGESTED = datetime(2026, 9, 17, 12, 0, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- time


def test_available_to_model_at_is_when_we_stored_it_not_when_it_was_published():
    """The whole point of Phase 4's time model.

    A document published eighteen months ago and fetched today became knowable
    today. If this ever starts returning the publication time, every backtest
    built on news silently gains foresight.
    """

    times = KnowledgeTimes.observed(
        first_seen_at=SEEN,
        ingested_at=INGESTED,
        source_published_at=PUBLISHED,
        source_published_precision=TimePrecision.EXACT,
    )

    assert times.available_to_model_at == INGESTED
    assert times.available_to_model_at != PUBLISHED
    assert times.source_published_at == PUBLISHED


def test_backfilling_a_year_of_releases_does_not_make_them_knowable_a_year_ago():
    backfilled = [
        KnowledgeTimes.observed(
            first_seen_at=SEEN,
            ingested_at=INGESTED,
            source_published_at=PUBLISHED + timedelta(days=offset),
            source_published_precision=TimePrecision.EXACT,
        )
        for offset in range(0, 360, 30)
    ]
    assert {t.available_to_model_at for t in backfilled} == {INGESTED}


def test_storing_before_noticing_is_rejected():
    with pytest.raises(TimeError, match="before noticing it"):
        KnowledgeTimes(
            system_first_seen_at=INGESTED,
            ingested_at=SEEN,
            available_to_model_at=INGESTED,
        )


def test_model_may_not_read_before_we_stored():
    with pytest.raises(TimeError, match="had not stored"):
        KnowledgeTimes(
            system_first_seen_at=SEEN,
            ingested_at=INGESTED,
            available_to_model_at=SEEN,
        )


def test_naive_timestamps_are_rejected():
    with pytest.raises(TimeError, match="timezone-aware"):
        KnowledgeTimes(
            system_first_seen_at=datetime(2026, 9, 17, 12, 0),
            ingested_at=INGESTED,
            available_to_model_at=INGESTED,
        )


def test_replay_assumption_requires_a_written_reason():
    times = KnowledgeTimes.observed(first_seen_at=SEEN, ingested_at=INGESTED)
    with pytest.raises(TimeError, match="needs a note"):
        times.with_replay_assumption(assumed_at=PUBLISHED, note="   ")

    annotated = times.with_replay_assumption(
        assumed_at=PUBLISHED + timedelta(minutes=15),
        note="assume a 15 minute collection lag from publication",
    )
    # The counterfactual sits beside the observation; it does not replace it.
    assert annotated.available_to_model_at == INGESTED
    assert annotated.replay_assumed_available_at == PUBLISHED + timedelta(minutes=15)


def test_exact_precision_without_a_timestamp_is_a_contradiction():
    with pytest.raises(TimeError, match="EXACT"):
        KnowledgeTimes(
            system_first_seen_at=SEEN,
            ingested_at=INGESTED,
            available_to_model_at=INGESTED,
            source_published_precision=TimePrecision.EXACT,
        )


def test_publication_after_first_sight_is_flagged_not_corrected():
    times = KnowledgeTimes.observed(
        first_seen_at=SEEN,
        ingested_at=INGESTED,
        source_published_at=SEEN + timedelta(hours=2),
        source_published_precision=TimePrecision.EXACT,
    )
    assert times.published_after_we_saw_it is True
    assert times.source_published_at == SEEN + timedelta(hours=2)


# --------------------------------------------------------------------------- feeds


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Ministry releases</title>
    <item>
      <title>Policy statement</title>
      <link>https://example.gov/a</link>
      <guid>https://example.gov/a</guid>
      <pubDate>Wed, 17 Sep 2026 08:30:00 +0900</pubDate>
      <description>Summary text</description>
    </item>
    <item>
      <title>Statistics release</title>
      <link>https://example.gov/b</link>
      <guid>urn:example:b</guid>
      <pubDate>Tue, 16 Sep 2026 15:00:00 +0900</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Agency</title>
  <entry>
    <title>Enforcement action</title>
    <link rel="alternate" href="https://example.gov/x"/>
    <id>tag:example.gov,2026:x</id>
    <published>2026-09-17T04:00:00Z</published>
    <updated>2026-09-17T06:00:00Z</updated>
    <summary>Short summary</summary>
  </entry>
</feed>
"""


def test_rss_items_parse_with_exact_times():
    items = parse_feed(RSS)
    assert [i.title for i in items] == ["Policy statement", "Statistics release"]
    assert items[0].published_at == datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    assert items[0].published_precision is TimePrecision.EXACT
    assert items[1].source_document_id == "urn:example:b"


def test_atom_prefers_published_over_updated():
    """An edited item stays anchored to when it was released.

    Using ``updated`` would move a release forward every time a typo was fixed,
    which is the wrong answer for "when did the market learn this".
    """

    items = parse_feed(ATOM)
    assert len(items) == 1
    assert items[0].published_at == datetime(2026, 9, 17, 4, 0, tzinfo=UTC)
    assert items[0].source_document_id == "tag:example.gov,2026:x"
    assert items[0].url == "https://example.gov/x"


def test_date_only_publication_is_not_promoted_to_a_minute():
    parsed, precision = parse_datetime("2026-09-17", assume_tz=JST)
    assert precision is TimePrecision.DATE_ONLY
    assert parsed == datetime(2026, 9, 17, 0, 0, tzinfo=JST)


def test_slash_dates_are_read_as_dates():
    parsed, precision = parse_datetime("2026/9/17", assume_tz=JST)
    assert precision is TimePrecision.DATE_ONLY
    assert parsed.date().isoformat() == "2026-09-17"


def test_local_time_without_an_offset_is_marked_inferred():
    parsed, precision = parse_datetime("2026-09-17T08:30:00")
    assert precision is TimePrecision.INFERRED
    assert parsed.tzinfo is not None


def test_unparseable_time_is_unknown_not_a_guess():
    parsed, precision = parse_datetime("sometime last week")
    assert parsed is None
    assert precision is TimePrecision.UNKNOWN


def test_feed_declaring_entities_is_refused():
    hostile = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss version="2.0"><channel/></rss>'
    with pytest.raises(FeedError, match="DOCTYPE or ENTITY"):
        parse_feed(hostile)


def test_non_feed_payload_is_refused():
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>not a feed</body></html>")


# ----------------------------------------------------------------------- licensing


SPEC = SourceSpec(
    source_key="example_gov",
    name="Example ministry",
    scope="JP",
    source_kind=SourceKind.GOVERNMENT,
    access_mechanism=AccessMechanism.RSS,
    official_url="https://example.gov/",
    feed_url="https://example.gov/rss.xml",
    auth_requirement=AuthRequirement.NONE,
    default_document_type=DocumentType.PRESS_RELEASE,
)


def policy(**overrides) -> SourcePolicy:
    base = {
        "source_key": "example_gov",
        "policy_version": "test-1.0.0",
        "license_mode": LicenseMode.PUBLIC_REDISTRIBUTABLE,
        "metadata_storage_allowed": Permission.ALLOWED,
        "full_text_storage_allowed": Permission.ALLOWED,
        "robots_allows_path": Permission.ALLOWED,
        "deciding_clause": "the standard terms permit reuse",
        "terms_url": "https://example.gov/terms",
    }
    base.update(overrides)
    return SourcePolicy(**base)


def test_source_that_does_not_permit_metadata_storage_is_refused_before_fetching():
    with pytest.raises(LicenseViolation, match="even metadata"):
        policy(metadata_storage_allowed=Permission.NOT_SPECIFIED).assert_may_collect()


def test_robots_prohibition_stops_collection():
    with pytest.raises(LicenseViolation, match="robots.txt"):
        policy(robots_allows_path=Permission.PROHIBITED).assert_may_collect()


@pytest.mark.parametrize(
    "permission",
    [Permission.PROHIBITED, Permission.NOT_SPECIFIED, Permission.UNKNOWN],
)
def test_only_an_explicit_permission_unlocks_full_text(permission):
    """Silence is not consent, here as everywhere else in this project."""

    storage, reason = policy(full_text_storage_allowed=permission).decide_body_storage()
    assert storage is BodyStorage.METADATA_ONLY
    assert permission.value in reason
    assert "Deciding clause" in reason


def test_explicit_permission_keeps_the_text():
    storage, reason = policy().decide_body_storage()
    assert storage is BodyStorage.FULL_TEXT
    assert "test-1.0.0" in reason


# ----------------------------------------------------------------------- collecting


class FakeSource:
    def __init__(self, items, bodies=None, fail_listing=False, fail_body_for=()):
        self.spec = SPEC
        self._items = items
        self._bodies = bodies or {}
        self._fail_listing = fail_listing
        self._fail_body_for = set(fail_body_for)
        self.body_calls: list[str] = []

    def list_items(self, *, since=None):
        if self._fail_listing:
            raise TimeoutError("the source did not answer")
        return self._items

    def fetch_body(self, item):
        self.body_calls.append(item.source_document_id)
        if item.source_document_id in self._fail_body_for:
            raise OSError("body fetch refused")
        return self._bodies.get(item.source_document_id)


def item(doc_id="doc-1", title="Title", url="https://example.gov/a", published=PUBLISHED, precision=TimePrecision.EXACT):
    return FeedItem(
        source_document_id=doc_id,
        title=title,
        url=url,
        published_at=published,
        published_precision=precision,
        summary="summary",
    )


def test_collecting_records_the_honest_knowledge_time():
    result = collect(
        FakeSource([item()], bodies={"doc-1": "full body"}),
        policy(),
        now=INGESTED,
    )
    assert result.items_new == 1
    document = result.documents[0]
    assert document.times.available_to_model_at == INGESTED
    assert document.times.source_published_at == PUBLISHED
    assert document.body_storage is BodyStorage.FULL_TEXT
    assert document.body_text == "full body"


def test_metadata_only_source_never_carries_the_body_it_fetched():
    source = FakeSource([item()], bodies={"doc-1": "full body"})
    result = collect(source, policy(full_text_storage_allowed=Permission.PROHIBITED), now=INGESTED)

    assert result.documents[0].body_storage is BodyStorage.METADATA_ONLY
    assert result.documents[0].body_text is None
    # And it did not even ask for the body it was not allowed to keep.
    assert source.body_calls == []


def test_unchanged_document_is_a_duplicate_not_a_revision():
    known_hash = content_fingerprint(title="Title", url="https://example.gov/a", body="full body", summary="summary")
    result = collect(
        FakeSource([item()], bodies={"doc-1": "full body"}),
        policy(),
        known={"doc-1": KnownDocument(content_sha256=known_hash, revision_seq=1)},
        now=INGESTED,
    )
    assert result.items_duplicate == 1
    assert result.items_new == 0
    assert result.documents == []


def test_amended_document_becomes_a_new_revision_and_points_at_the_old_one():
    result = collect(
        FakeSource([item(title="Title (amended)")], bodies={"doc-1": "full body"}),
        policy(),
        known={"doc-1": KnownDocument(content_sha256="stale-hash", revision_seq=1, document_id="old-uuid")},
        now=INGESTED,
    )
    assert result.items_revised == 1
    assert result.documents[0].revision_seq == 2
    assert result.documents[0].supersedes_document_id == "old-uuid"


def test_empty_listing_is_recorded_not_treated_as_success_or_failure():
    result = collect(FakeSource([]), policy(), now=INGESTED)
    assert result.items_listed == 0
    assert result.fetch_errors == 0
    assert result.coverage_quality == "UNKNOWN"
    assert any("listed nothing" in note for note in result.notes)


def test_listing_failure_is_reported_as_a_gap_not_an_empty_day():
    result = collect(FakeSource([], fail_listing=True), policy(), now=INGESTED)
    assert result.fetch_errors == 1
    assert result.coverage_quality == "PARTIAL_KNOWN_GAP"
    assert any("listing failed" in note for note in result.notes)


def test_one_failed_body_does_not_lose_the_other_documents():
    source = FakeSource(
        [item("doc-1"), item("doc-2", url="https://example.gov/b")],
        bodies={"doc-2": "second body"},
        fail_body_for=("doc-1",),
    )
    result = collect(source, policy(), now=INGESTED)
    assert result.fetch_errors == 1
    assert [d.source_document_id for d in result.documents] == ["doc-2"]
    assert result.coverage_quality == "PARTIAL_KNOWN_GAP"


def test_permitted_body_that_comes_back_empty_still_records_the_document():
    """Existence and timing are information even when the text is missing."""

    result = collect(FakeSource([item()], bodies={}), policy(), now=INGESTED)
    assert result.items_new == 1
    assert result.documents[0].body_storage is BodyStorage.METADATA_ONLY
    assert any("no body returned" in warning for warning in result.quality_warnings)


def test_missing_publication_time_is_a_quality_warning_not_a_fabricated_time():
    result = collect(
        FakeSource([item(published=None, precision=TimePrecision.UNKNOWN)], bodies={"doc-1": "b"}),
        policy(),
        now=INGESTED,
    )
    assert result.documents[0].times.source_published_at is None
    assert any("no usable publication timestamp" in w for w in result.quality_warnings)


def test_document_claiming_no_body_cannot_smuggle_one():
    with pytest.raises(ValueError, match="must not be carrying text"):
        CollectedDocument(
            source_key="example_gov",
            source_document_id="doc-1",
            document_type=DocumentType.PRESS_RELEASE,
            times=KnowledgeTimes.observed(first_seen_at=SEEN, ingested_at=INGESTED),
            content_sha256="abc",
            body_storage=BodyStorage.METADATA_ONLY,
            body_text="smuggled",
        )


def test_fingerprint_covers_only_what_we_hold():
    """A metadata-only row cannot claim to notice a body change it never saw."""

    without_body = content_fingerprint(title="T", url="u", body=None, summary="s")
    with_body = content_fingerprint(title="T", url="u", body="text", summary="s")
    assert without_body != with_body
    assert without_body == content_fingerprint(title="T", url="u", body=None, summary="s")


RSS10 = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns="http://purl.org/rss/1.0/"
         xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dc="http://purl.org/dc/elements/1.1/" xml:lang="ja">
<channel rdf:about="https://example.go.jp/index.html">
  <title>Agency</title>
  <items><rdf:Seq><rdf:li rdf:resource="https://example.go.jp/a.html"/></rdf:Seq></items>
</channel>
<item rdf:about="https://example.go.jp/a.html">
  <title>Machinery orders</title>
  <link>https://example.go.jp/a.html</link>
  <description>Summary</description>
  <dc:date>2026-09-16T08:50:00+09:00</dc:date>
</item>
</rdf:RDF>
"""


def test_rss_1_0_items_parse_despite_their_namespace():
    """Found by live data, not by review.

    RSS 1.0 puts title, link and description in the RSS 1.0 namespace. Looking
    for the bare names finds nothing, and a feed that parses to zero items is
    indistinguishable from a source that published nothing that day - which is
    the failure mode this whole file is built to keep visible.
    """

    items = parse_feed(RSS10, assume_tz=JST)

    assert len(items) == 1
    assert items[0].title == "Machinery orders"
    assert items[0].url == "https://example.go.jp/a.html"
    assert items[0].summary == "Summary"
    assert items[0].published_precision is TimePrecision.EXACT
    assert items[0].published_at == datetime(2026, 9, 15, 23, 50, tzinfo=UTC)


def test_an_rss_1_0_item_without_a_guid_is_identified_by_rdf_about():
    without_link = RSS10.replace(b"<link>https://example.go.jp/a.html</link>", b"")
    items = parse_feed(without_link, assume_tz=JST)
    assert items[0].source_document_id == "https://example.go.jp/a.html"
