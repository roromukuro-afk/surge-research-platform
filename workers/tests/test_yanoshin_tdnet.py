"""Yanoshin TDnet discovery: code normalisation, the cursor, and the classifier.

Two of these test behaviour that only live data revealed, and both are the kind
that fails silently: a documented parameter that does not do what it says, and a
five-character code whose fifth character is not a suffix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from surge.material.tdnet_classify import CLASSIFIER_VERSION, DisclosureType, classify, summarise
from surge.news.feeds import JST
from surge.news.sources.yanoshin_tdnet import (
    CodeNormalisation,
    YanoshinError,
    YanoshinTdnetSource,
    normalise_company_code,
    parse_response,
    recovery_window,
)

FETCHED = datetime(2026, 9, 17, 14, 0, tzinfo=JST)


# ------------------------------------------------------------ code normalisation


@pytest.mark.parametrize(
    ("raw", "expected", "kind"),
    [
        # A fifth character of "0" is the ordinary-share suffix and carries
        # nothing, so it goes.
        ("72030", "7203", CodeNormalisation.FIVE_CHAR_TRAILING_ZERO_STRIPPED),
        ("130A0", "130A", CodeNormalisation.FIVE_CHAR_TRAILING_ZERO_STRIPPED),
        ("92730", "9273", CodeNormalisation.FIVE_CHAR_TRAILING_ZERO_STRIPPED),
        ("133A0", "133A", CodeNormalisation.FIVE_CHAR_TRAILING_ZERO_STRIPPED),
        # A non-zero fifth character marks a non-ordinary security. Stripping it
        # would merge an ETF with whatever four-digit issuer shares its prefix.
        ("587A4", "587A4", CodeNormalisation.FIVE_CHAR_KEPT),
        ("13264", "13264", CodeNormalisation.FIVE_CHAR_KEPT),
        ("15574", "15574", CodeNormalisation.FIVE_CHAR_KEPT),
        ("16714", "16714", CodeNormalisation.FIVE_CHAR_KEPT),
        ("7203", "7203", CodeNormalisation.ALREADY_FOUR_CHAR),
        ("130A", "130A", CodeNormalisation.ALREADY_FOUR_CHAR),
    ],
)
def test_company_codes_normalise_by_the_fifth_character_not_by_the_zero(raw, expected, kind):
    """All ten cases came off one live page, including every KEPT one."""

    result = normalise_company_code(raw)
    assert result.normalised == expected
    assert result.kind is kind
    assert result.raw == raw


def test_the_raw_code_always_survives():
    """A normalisation that turns out to be wrong is only findable from its input."""

    assert normalise_company_code("587A4").raw == "587A4"
    assert normalise_company_code(" 72030 ").raw == "72030"


@pytest.mark.parametrize("raw", ["", None, "12", "1234567", "72-30"])
def test_an_unexpected_shape_is_flagged_rather_than_guessed(raw):
    result = normalise_company_code(raw)
    assert result.kind is CodeNormalisation.UNEXPECTED_SHAPE
    assert result.is_resolvable is False


# ------------------------------------------------------------------- parsing


# A str, encoded where it is used: a bytes literal cannot hold the Japanese
# titles, and the real payload is UTF-8 anyway.
_RESPONSE_TEXT = """{
 "total_count": 3,
 "condition_desc": "\\u6700\\u65b0\\u9806",
 "items": [
  {"Tdnet": {"id": "1281126", "pubdate": "2026-09-17 13:00:00", "company_code": "92730",
             "company_name": "Test Holdings", "title": "業績予想の修正に関するお知らせ",
             "document_url": "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/140120260916537315.pdf",
             "url_xbrl": null, "markets_string": "東", "update_history": null}},
  {"Tdnet": {"id": "1281124", "pubdate": "2026-09-17 12:20:00", "company_code": "587A4",
             "company_name": "Some ETN", "title": "インベスコQQQトラストに関する日々の開示事項",
             "document_url": null, "url_xbrl": "https://example.jp/x.zip",
             "markets_string": "東", "update_history": null}},
  {"Tdnet": {"id": "notanumber", "pubdate": "2026-09-17 11:00:00", "company_code": "12340",
             "title": "ignored"}}
 ]
}"""

RESPONSE = _RESPONSE_TEXT.encode("utf-8")


def test_the_nesting_key_is_tdnet_not_TDnet():
    """The published llms.txt says "TDnet". The service sends "Tdnet".

    A parser that trusted the documentation would return zero items on every
    call, which looks exactly like a day on which nothing was disclosed.
    """

    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert len(result.items) == 2
    assert result.total_count == 3


def test_a_row_without_a_numeric_id_is_dropped_not_guessed():
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert [item.yanoshin_id for item in result.items] == [1281126, 1281124]


def test_pubdate_is_read_as_tokyo_time():
    """The payload carries no offset. Reading it as UTC moves every disclosure
    nine hours earlier - across the close, for anything published in the
    afternoon."""

    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert result.items[0].pubdate == datetime(2026, 9, 17, 13, 0, tzinfo=JST)
    assert result.items[0].pubdate.astimezone(UTC).hour == 4


def test_the_document_url_is_unwrapped_from_the_redirect():
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert result.items[0].tdnet_document_url == (
        "https://www.release.tdnet.info/inbs/140120260916537315.pdf"
    )


def test_collection_lag_is_measured_from_the_newest_item():
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    # newest pubdate 13:00, fetched 14:00
    assert result.collection_lag_seconds() == pytest.approx(3600.0)


def test_the_response_hash_covers_the_whole_payload():
    first = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    second = parse_response(RESPONSE.replace(b'"total_count": 3', b'"total_count": 4'), endpoint="e", fetched_at=FETCHED)
    assert len(first.response_sha256) == 64
    assert first.response_sha256 != second.response_sha256


# -------------------------------------------------------------------- cursor


def test_since_id_and_by_id_are_refused():
    """Documented, accepted by the service, and parsed as Unix timestamps.

    since_id=1281122 becomes "period starting 1970-01-16" and filters nothing;
    by_id becomes "period ending 1970-01-16" and returns zero rows. Measured
    2026-09-17. Refusing them here stops a well-meaning caller reinstating the
    documented behaviour that does not exist.
    """

    source = YanoshinTdnetSource()
    with pytest.raises(YanoshinError, match="Unix timestamps"):
        source._get("recent", since_id=1281122)
    with pytest.raises(YanoshinError, match="Unix timestamps"):
        source._get("recent", by_id=1281122)


def test_the_cursor_filters_client_side():
    source = YanoshinTdnetSource(last_seen_id=1281124)
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    filtered = source._apply_cursor(result)
    assert [item.yanoshin_id for item in filtered.items] == [1281126]


def test_the_cursor_never_moves_backwards():
    source = YanoshinTdnetSource(last_seen_id=1281126)
    older = parse_response(
        RESPONSE.replace(b'"1281126"', b'"1280000"').replace(b'"1281124"', b'"1279999"'),
        endpoint="e",
        fetched_at=FETCHED,
    )
    assert source.advance_cursor(older) == 1281126


def test_the_cursor_advances_to_the_highest_id_seen():
    source = YanoshinTdnetSource()
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert source.advance_cursor(result) == 1281126


def test_an_empty_page_leaves_the_cursor_alone():
    source = YanoshinTdnetSource(last_seen_id=999)
    empty = parse_response(b'{"items": []}', endpoint="e", fetched_at=FETCHED)
    assert source.advance_cursor(empty) == 999


def test_a_full_page_is_not_treated_as_a_complete_window():
    """300 items came back spanning 2.3 days when measured, so a full page may
    have been truncated and something older than its oldest row may have been
    dropped. The caller has to fall back to a date range rather than assume the
    gap is closed."""

    source = YanoshinTdnetSource(limit=2)
    full = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert source.window_is_safe(full) is False

    source = YanoshinTdnetSource(limit=300)
    assert source.window_is_safe(full) is True


def test_the_recovery_window_reaches_back_past_the_last_success():
    start, end = recovery_window(datetime(2026, 9, 15, 9, 0, tzinfo=JST), FETCHED)
    # A day of overlap: re-reading a day costs one request and some duplicates
    # the collector already drops; missing one leaves a silent hole.
    assert start == date(2026, 9, 14)
    assert end == date(2026, 9, 17)


def test_the_recovery_window_is_bounded():
    start, end = recovery_window(datetime(2020, 1, 1, tzinfo=JST), FETCHED, max_days=14)
    assert (end - start).days == 14


def test_a_first_run_asks_for_yesterday_and_today():
    start, end = recovery_window(None, FETCHED)
    assert start == date(2026, 9, 16)
    assert end == date(2026, 9, 17)


def test_a_backwards_date_range_is_refused():
    with pytest.raises(YanoshinError, match="before start"):
        YanoshinTdnetSource().fetch_date_range(date(2026, 9, 17), date(2026, 9, 16))


# ------------------------------------------------------------------ the body


def test_the_adapter_never_returns_a_body():
    """The licence, not caution. Reaching an index that links to a TDnet PDF
    licenses nothing about storing the PDF, and news.source_policies marks full
    text PROHIBITED for this source so the database refuses it too."""

    source = YanoshinTdnetSource()
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    assert source.fetch_body(result.items[0].as_feed_item()) is None


def test_the_feed_item_carries_no_summary():
    result = parse_response(RESPONSE, endpoint="e", fetched_at=FETCHED)
    item = result.items[0].as_feed_item()
    assert item.summary is None
    assert item.title == "業績予想の修正に関するお知らせ"
    assert item.raw_fields["raw_company_code"] == "92730"


# -------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # 自己株式 takes four different verbs and they are four different events.
        ("自己株式の取得状況に関するお知らせ", DisclosureType.BUYBACK),
        ("自己株式立会外買付取引（ToSTNeT-3）による自己株式の買付けに関するお知らせ", DisclosureType.BUYBACK),
        ("自己株式の消却に関するお知らせ", DisclosureType.TREASURY_CANCELLATION),
        ("譲渡制限付株式報酬としての自己株式処分に関するお知らせ", DisclosureType.TREASURY_DISPOSAL),
        # Both end in 予想の修正; only the prefix separates them.
        ("2026年９月期業績予想の修正に関するお知らせ", DisclosureType.EARNINGS_FORECAST_REVISION),
        ("配当予想の修正に関するお知らせ", DisclosureType.DIVIDEND_FORECAST_REVISION),
        # A first publication is not a revision.
        ("連結決算への移行に伴う連結業績予想の公表に関するお知らせ", DisclosureType.EARNINGS_FORECAST),
        # Only 短信 is the release itself.
        ("2026年8月期決算短信", DisclosureType.EARNINGS_RESULT),
        ("2026年10月期 第３四半期決算説明資料", DisclosureType.EARNINGS_MATERIAL),
        # 異動 means whatever precedes it.
        ("親会社以外の支配株主及び主要株主の異動に関するお知らせ", DisclosureType.MAJOR_SHAREHOLDER_CHANGE),
        ("公認会計士等の異動および一時会計監査人の選任に関するお知らせ", DisclosureType.AUDITOR_CHANGE),
        ("持分法適用関連会社の異動（株式譲渡）に関するお知らせ", DisclosureType.MA_SUBSIDIARY),
        # An asset sale that books a gain is typed by the gain.
        ("不動産の売却および特別利益（固定資産売却益）の計上に関するお知らせ", DisclosureType.EXTRAORDINARY_GAIN),
        ("販売用不動産の売却に関するお知らせ", DisclosureType.ASSET_TRANSACTION),
        ("熱延工場における火災発生に関するお知らせとお詫び", DisclosureType.INCIDENT),
        ("株式会社カカクコムに対する公開買付けの開始予定", DisclosureType.TENDER_OFFER),
        ("インベスコQQQトラスト シリーズ1 に関する日々の開示事項", DisclosureType.ETF_DAILY),
        ("ETFの収益分配金見込額のお知らせ", DisclosureType.ETF_DISTRIBUTION),
        ("2026年8月度　ＫＰＩのお知らせ", DisclosureType.MONTHLY_UPDATE),
        ("資本コストや株価を意識した経営の実現に向けた対応について", DisclosureType.GOVERNANCE),
    ],
)
def test_titles_from_live_data_classify_as_expected(title, expected):
    assert classify(title).disclosure_type is expected


def test_an_intra_group_dividend_receipt_is_not_a_dividend():
    """配当金受領 is money arriving from a subsidiary, not a distribution to
    shareholders. A pattern anchored on 配当 alone would type it as one."""

    assert classify("連結子会社からの配当金受領に関するお知らせ").disclosure_type is not DisclosureType.DIVIDEND


def test_a_correction_keeps_the_type_of_what_it_corrects():
    result = classify("（訂正）「2027年3月期 第1四半期決算短信〔IFRS〕」の一部訂正に関するお知らせ")
    assert result.disclosure_type is DisclosureType.EARNINGS_RESULT
    assert result.is_correction is True


def test_a_progress_update_is_a_flag_not_a_type():
    result = classify("（開示事項の経過）新規事業の開始及び資金使途の充当順位変更に関するお知らせ")
    assert result.is_progress_update is True
    assert result.is_correction is False


def test_an_unrecognised_title_is_typed_OTHER_and_still_carried():
    """The classifier never excludes. Relevance is a Phase 5 decision made from
    structural signals, not a lexical one made here."""

    result = classify("公益財団法人 財務会計基準機構への加入状況について")
    assert result.disclosure_type is DisclosureType.OTHER
    assert result.matched_pattern is None


def test_every_classification_is_provisional_and_carries_its_evidence():
    result = classify("2026年８月期決算短信")
    assert result.confidence == "PROVISIONAL"
    evidence = result.as_evidence()
    assert evidence["classifier_version"] == CLASSIFIER_VERSION
    assert evidence["matched_pattern"] == "決算短信"


def test_routine_types_are_marked_rather_than_dropped():
    routine = classify("ETFの収益分配金見込額のお知らせ")
    assert routine.is_routine is True
    material = classify("業績予想の修正に関するお知らせ")
    assert material.is_routine is False


def test_the_summary_counts_flags_separately_from_types():
    counts = summarise(
        [
            classify("2026年8月期決算短信"),
            classify("（訂正）2026年8月期決算短信"),
            classify("ETFの収益分配金見込額のお知らせ"),
        ]
    )
    assert counts["EARNINGS_RESULT"] == 2
    assert counts["_corrections"] == 1
    assert counts["_routine"] == 1
