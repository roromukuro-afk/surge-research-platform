"""Phase 2.1 provider adapters, against fixed payloads.

The fixtures here are hand-written from the shapes the live APIs actually
returned on 2026-09-16 (ECB and OpenFIGI) or from the providers' documented
field names (J-Quants and EODHD). They exist to pin behaviour that is easy to
get quietly wrong: which leg divides which, what an ambiguous mapping does, and
which column is not raw.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from surge.providers.ecb_fx import (
    DERIVATION_METHOD,
    _parse_csvdata,
    derive_usdjpy,
)
from surge.providers.eodhd import (
    ADJUSTED_CLOSE_BASIS,
    BULK_EXCHANGE_CALL_COST,
    OHLC_BASIS,
    VOLUME_BASIS,
    QuotaLedger,
    parse_bulk_bar,
    parse_dividend,
    parse_split,
    parse_symbol,
)
from surge.providers.jquants import (
    JQuantsFieldError,
    extract_records,
    parse_bar,
    parse_master,
)
from surge.providers.openfigi import (
    MappingRequest,
    MappingStatus,
    _interpret,
    coverage,
    ticker_candidates,
)

# ------------------------------------------------------------------------ ECB
ECB_HEADER = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,"
    "OBS_STATUS,OBS_CONF,OBS_PRE_BREAK,OBS_COM,TIME_FORMAT,BREAKS,COLLECTION,COMPILING_ORG,"
    "DISS_ORG,DOM_SER_IDS,PUBL_ECB,PUBL_MU,PUBL_PUBLIC,UNIT_INDEX_BASE,COMPILATION,COVERAGE,"
    "DECIMALS,NAT_TITLE,SOURCE_AGENCY,SOURCE_PUB,TITLE,TITLE_COMPL,UNIT,UNIT_MULT"
)


def _ecb_row(series: str, currency: str, period: str, value: str, decimals: str) -> str:
    return (
        f"{series},D,{currency},EUR,SP00,A,{period},{value},A,F,,,P1D,,A,,,,,,,99Q1=100,,,"
        f"{decimals},,4F0,,{currency}/Euro ECB reference exchange rate,"
        f'"ECB reference exchange rate, {currency}/Euro, 2.15 pm (C.E.T.)",{currency},0'
    )


def _ecb_body(rows: list[str]) -> bytes:
    return ("\n".join([ECB_HEADER, *rows]) + "\n").encode("utf-8")


def test_the_cross_divides_the_yen_leg_by_the_dollar_leg():
    """USD/JPY is JPY-per-EUR over USD-per-EUR. Inverting it is a silent 4x error."""

    body = _ecb_body(
        [
            _ecb_row("EXR.D.USD.EUR.SP00.A", "USD", "2026-09-16", "1.1537", "4"),
            _ecb_row("EXR.D.JPY.EUR.SP00.A", "JPY", "2026-09-16", "178.88", "2"),
        ]
    )
    rates = derive_usdjpy(_parse_csvdata(body))

    assert len(rates) == 1
    rate = rates[0]
    assert rate.source_date == date(2026, 9, 16)
    assert rate.eur_usd == Decimal("1.1537")
    assert rate.eur_jpy == Decimal("178.88")
    assert rate.derived_usd_jpy == Decimal("155.048973")
    assert rate.derivation_method == DERIVATION_METHOD


def test_the_two_legs_keep_their_own_precision():
    """The ECB publishes USD/EUR to 4 decimals and JPY/EUR to 2.

    That asymmetry is the reason both legs are stored: the cross inherits the
    coarser one, and only the legs can explain the residual.
    """

    body = _ecb_body(
        [
            _ecb_row("EXR.D.USD.EUR.SP00.A", "USD", "2026-09-16", "1.1537", "4"),
            _ecb_row("EXR.D.JPY.EUR.SP00.A", "JPY", "2026-09-16", "178.88", "2"),
        ]
    )
    rate = derive_usdjpy(_parse_csvdata(body))[0]
    assert rate.eur_usd_decimals == 4
    assert rate.eur_jpy_decimals == 2


def test_a_day_with_one_leg_produces_no_cross():
    """Half a pair is not a rate, and must not borrow the other day's leg."""

    body = _ecb_body(
        [
            _ecb_row("EXR.D.USD.EUR.SP00.A", "USD", "2026-09-15", "1.1539", "4"),
            _ecb_row("EXR.D.JPY.EUR.SP00.A", "JPY", "2026-09-15", "178.86", "2"),
            _ecb_row("EXR.D.USD.EUR.SP00.A", "USD", "2026-09-16", "1.1537", "4"),
        ]
    )
    rates = {r.source_date: r for r in derive_usdjpy(_parse_csvdata(body))}

    assert rates[date(2026, 9, 15)].is_complete is True
    incomplete = rates[date(2026, 9, 16)]
    assert incomplete.is_complete is False
    assert incomplete.derived_usd_jpy is None
    assert "JPY/EUR" in incomplete.note


def test_a_published_gap_is_skipped_rather_than_read_as_zero():
    body = _ecb_body(
        [
            _ecb_row("EXR.D.USD.EUR.SP00.A", "USD", "2026-09-16", "", "4"),
            _ecb_row("EXR.D.JPY.EUR.SP00.A", "JPY", "2026-09-16", "", "2"),
        ]
    )
    assert _parse_csvdata(body) == []


def test_columns_are_read_by_name_not_position():
    """The ECB can add attribute columns; a positional parser would break silently."""

    header = "TIME_PERIOD,OBS_VALUE,KEY,CURRENCY,DECIMALS,OBS_STATUS,TITLE_COMPL,A_NEW_COLUMN"
    body = (
        header
        + "\n2026-09-16,178.88,EXR.D.JPY.EUR.SP00.A,JPY,2,A,whatever,surprise\n"
    ).encode("utf-8")
    observations = _parse_csvdata(body)
    assert observations[0].value == Decimal("178.88")
    assert observations[0].decimals == 2


# ------------------------------------------------------------------- OpenFIGI
def test_one_match_is_exact_and_carries_a_share_class_figi():
    outcome = _interpret(
        MappingRequest("TICKER", "AAPL", exch_code="US"),
        {
            "data": [
                {
                    "figi": "BBG000B9XRY4",
                    "compositeFIGI": "BBG000B9XRY4",
                    "shareClassFIGI": "BBG001S5N8V8",
                    "ticker": "AAPL",
                    "exchCode": "US",
                    "securityType": "Common Stock",
                    "securityType2": "Common Stock",
                    "name": "APPLE INC",
                    "marketSector": "Equity",
                }
            ]
        },
    )
    assert outcome.status is MappingStatus.EXACT
    assert outcome.share_class_figi == "BBG001S5N8V8"


def test_two_matches_are_ambiguous_and_refuse_to_pick_one():
    """FRCB really does return two different issuers. Picking the first is a false merge."""

    outcome = _interpret(
        MappingRequest("TICKER", "FRCB", exch_code="US", include_unlisted_equities=True),
        {
            "data": [
                {"figi": "BBG000KXB3Z7", "shareClassFIGI": "BBG001SD41D7", "name": "FIRST CONTL BANCSHARES INC"},
                {"figi": "BBG0019LSQ49", "shareClassFIGI": "BBG001TG3G38", "name": "FIRST REPUBLIC BANK/CA"},
            ]
        },
    )
    assert outcome.status is MappingStatus.AMBIGUOUS
    assert outcome.match_count == 2
    assert outcome.share_class_figi is None


def test_a_warning_is_unmapped_and_an_error_is_an_error():
    request = MappingRequest("TICKER", "ZZZZ", exch_code="US")
    assert _interpret(request, {"warning": "No identifier found."}).status is MappingStatus.UNMAPPED
    assert _interpret(request, {"error": "Invalid idType."}).status is MappingStatus.ERROR


def test_share_class_punctuation_candidates():
    """OpenFIGI writes BRK/B; the SEC writes BRK-B; other feeds write BRK.B."""

    assert ticker_candidates("BRK-B") == ["BRK-B", "BRK/B"]
    assert ticker_candidates("BRK.B") == ["BRK.B", "BRK/B"]
    assert ticker_candidates("AAPL") == ["AAPL"]


def test_coverage_counts_every_status():
    request = MappingRequest("TICKER", "X", exch_code="US")
    outcomes = [
        _interpret(request, {"data": [{"figi": "A"}]}),
        _interpret(request, {"data": [{"figi": "A"}, {"figi": "B"}]}),
        _interpret(request, {"warning": "No identifier found."}),
    ]
    assert coverage(outcomes) == {
        "inputs": 3,
        "exact": 1,
        "ambiguous": 1,
        "unmapped": 1,
        "error": 0,
    }


# ------------------------------------------------------------------ J-Quants
def test_a_bar_keeps_raw_and_adjusted_apart():
    record = {
        "Code": "13010",
        "Date": "2026-09-16",
        "O": "1000", "H": "1100", "L": "990", "C": "1050", "Vo": "12345", "Va": "12800000",
        "AdjO": "500", "AdjH": "550", "AdjL": "495", "AdjC": "525", "AdjVo": "24690",
        "AdjFactor": "0.5", "ExRT": "1",
    }
    bar = parse_bar(record)

    assert bar.open == Decimal("1000")
    assert bar.adj_open == Decimal("500")
    assert bar.adjustment_factor == Decimal("0.5")
    assert bar.ex_event_code == "1"
    assert bar.is_five_digit_code is True


def test_the_v1_field_names_are_accepted_too():
    """The v2 short names could not be confirmed without a subscription."""

    bar = parse_bar(
        {"Code": "1301", "Date": "2026-09-16", "Open": "1000", "Close": "1050", "Volume": "10"}
    )
    assert bar.open == Decimal("1000")
    assert bar.close == Decimal("1050")
    assert bar.is_five_digit_code is False


def test_a_missing_required_field_names_the_keys_it_did_get():
    with pytest.raises(JQuantsFieldError, match="keys are"):
        parse_bar({"Date": "2026-09-16", "O": "1"})


def test_the_record_list_is_found_whatever_the_envelope_is_called():
    records, key = extract_records({"daily_quotes": [{"Code": "1301"}], "pagination_key": "abc"})
    assert key == "daily_quotes"
    assert records == [{"Code": "1301"}]

    records, key = extract_records({"data": [{"Code": "1301"}]})
    assert key == "data"


def test_an_ambiguous_envelope_is_refused_rather_than_guessed():
    with pytest.raises(JQuantsFieldError, match="more than one candidate"):
        extract_records({"a": [{"x": 1}], "b": [{"y": 2}]})
    with pytest.raises(JQuantsFieldError, match="no record list"):
        extract_records({"pagination_key": "abc"})


def test_master_records_parse():
    record = {"Code": "13010", "CoName": "極洋", "Mkt": "0111", "S33": "0050", "ScaleCat": "TOPIX Small 2"}
    master = parse_master(record)
    assert master.code == "13010"
    assert master.name == "極洋"
    assert master.market_code == "0111"


# --------------------------------------------------------------------- EODHD
def test_the_documented_bases_say_volume_is_not_raw():
    """The single most dangerous fact about this provider, asserted."""

    assert OHLC_BASIS == "RAW"
    assert VOLUME_BASIS == "SPLIT_ADJUSTED"
    assert ADJUSTED_CLOSE_BASIS == "SPLIT_AND_DIVIDEND_ADJUSTED"


def test_a_bulk_bar_parses_from_either_case_convention():
    json_style = parse_bulk_bar(
        {
            "code": "AAPL", "exchange_short_name": "NASDAQ", "date": "2026-09-16",
            "open": "230.1", "high": "232.0", "low": "229.5", "close": "231.4",
            "adjusted_close": "231.4", "volume": "45000000",
        }
    )
    csv_style = parse_bulk_bar(
        {
            "Code": "AAPL", "Ex": "NASDAQ", "Date": "2026-09-16",
            "Open": "230.1", "High": "232.0", "Low": "229.5", "Close": "231.4",
            "Adjusted_close": "231.4", "Volume": "45000000",
        }
    )
    assert json_style.code == csv_style.code == "AAPL"
    assert json_style.trade_date == csv_style.trade_date == date(2026, 9, 16)
    assert json_style.volume == csv_style.volume == Decimal("45000000")


def test_a_split_is_read_as_new_over_old():
    split = parse_split("AAPL", {"date": "2020-08-31", "split": "4.000000/1.000000"})
    assert split.ex_date == date(2020, 8, 31)
    assert split.split_to == Decimal("4.000000")
    assert split.split_from == Decimal("1.000000")
    assert split.raw_split == "4.000000/1.000000"


def test_an_unparseable_split_keeps_the_raw_string():
    """Better an unusable row that shows what arrived than a fabricated ratio."""

    split = parse_split("AAPL", {"date": "2020-08-31", "split": "weird"})
    assert split.split_to is None
    assert split.split_from is None
    assert split.raw_split == "weird"


def test_a_dividend_keeps_both_amounts():
    dividend = parse_dividend(
        "AAPL",
        {
            "date": "2026-08-08", "value": "0.25", "unadjustedValue": "0.25",
            "currency": "USD", "declarationDate": "2026-07-31",
            "recordDate": "2026-08-11", "paymentDate": "2026-08-14", "period": "Quarterly",
        },
    )
    assert dividend.value == Decimal("0.25")
    assert dividend.unadjusted_value == Decimal("0.25")
    assert dividend.payment_date == date(2026, 8, 14)


def test_a_symbol_row_parses():
    symbol = parse_symbol(
        {"Code": "AAPL", "Name": "Apple Inc", "Exchange": "NASDAQ", "Type": "Common Stock", "Isin": "US0378331005"}
    )
    assert symbol.code == "AAPL"
    assert symbol.isin == "US0378331005"


def test_the_quota_counts_api_calls_not_http_requests():
    """One bulk request is one HTTP call and one hundred quota calls."""

    ledger = QuotaLedger()
    ledger.charge("EODHD_US_EOD_BULK", BULK_EXCHANGE_CALL_COST)
    ledger.charge("EODHD_SPLITS", 1)

    assert ledger.requests == 2
    assert ledger.calls == 101
    assert ledger.by_dataset["EODHD_US_EOD_BULK"] == 100
