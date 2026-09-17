"""The three jobs that need no credential, wired together.

FX ingest, the 3,000 JPY filter and Stage 1 screening are the whole daily path
for a zero-cost core. They are tested here as a chain: prices and a rate go in,
a candidate list comes out, and every refusal on the way is visible.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from surge.jobs.fx_ingest import FxIngestJob, http_date, latest_observation, rate_params
from surge.jobs.screening import PriceEligibilityJob, Stage1ScreeningJob
from surge.licensing import AvailabilityBasis
from surge.market.eligibility import EligibilityDecision, FxObservation
from surge.market.models import CanonicalAction, CanonicalBar, CorporateActionType, PriceBasis
from surge.providers.ecb_fx import DERIVATION_METHOD, UsdJpyRate
from surge.storage.local import LocalObjectStore

NOW = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


def _bars(symbol: str, closes: list[str], *, market: str = "JP", start: date = date(2026, 1, 5)):
    bars = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price = Decimal(close)
        bars.append(
            CanonicalBar(
                provider_id="free", dataset_key="ECB_EXR_DAILY", market_code=market,
                native_symbol=symbol, trade_date=day, currency="JPY" if market == "JP" else "USD",
                open=price, high=price * Decimal("1.01"), low=price * Decimal("0.99"), close=price,
                volume=Decimal(1000), turnover=Decimal(100000),
                open_basis=PriceBasis.RAW, high_basis=PriceBasis.RAW, low_basis=PriceBasis.RAW,
                close_basis=PriceBasis.RAW, volume_basis=PriceBasis.RAW,
                observed_at=YESTERDAY, available_at=YESTERDAY,
                availability_basis=AvailabilityBasis.OBSERVED_NOW,
            )
        )
        day += timedelta(days=1)
    return bars


class RecordingWriter:
    def __init__(self) -> None:
        self.eligibility: list = []
        self.features: list = []
        self.candidates: list = []
        self.fx: list = []
        self.objects: list = []

    def write_eligibility(self, results, **kwargs):
        self.eligibility.append((results, kwargs))
        return len(results)

    def write_features(self, features, **kwargs):
        self.features.extend(features)
        return len(features)

    def write_candidates(self, candidates, **kwargs):
        self.candidates.extend(candidates)
        return len(candidates)

    def record_raw_object(self, record):
        self.objects.append(record)

    def write_fx(self, params):
        self.fx.append(params)


# ---------------------------------------------------------------- FX ingest
class FakeEcb:
    """Stands in for the live API so the chain can be tested deterministically."""

    def __init__(self, rates, *, not_modified: bool = False, raise_with: Exception | None = None):
        self._rates = rates
        self._not_modified = not_modified
        self._raise = raise_with
        self.last_if_modified_since: str | None = None

    def fetch(self, *, start=None, end=None, last_n=None, if_modified_since=None):
        self.last_if_modified_since = if_modified_since
        if self._raise:
            raise self._raise

        from surge.models import Provenance  # noqa: PLC0415
        from surge.providers.ecb_fx import EcbFetchResult  # noqa: PLC0415

        provenance = Provenance(
            source_id="ecb", dataset_key="ECB_EXR_DAILY", endpoint="https://example.invalid/exr",
            requested_at=NOW, received_at=NOW, http_status=304 if self._not_modified else 200,
            bytes=0 if self._not_modified else 100,
            content_sha256=None if self._not_modified else "a" * 64,
            item_count=len(self._rates) * 2, observed_at=NOW, available_at=NOW,
        )
        return EcbFetchResult(
            observations=[], rates=[] if self._not_modified else self._rates,
            provenance=provenance, body=b"" if self._not_modified else b"csv body",
            source_published_at=NOW - timedelta(hours=10), not_modified=self._not_modified,
        )


def _rate(day: date, usd: str, jpy: str, cross: str | None) -> UsdJpyRate:
    return UsdJpyRate(
        source_date=day, eur_usd=Decimal(usd), eur_jpy=Decimal(jpy),
        derived_usd_jpy=None if cross is None else Decimal(cross),
        eur_usd_decimals=4, eur_jpy_decimals=2, derivation_method=DERIVATION_METHOD,
        note=None if cross else "no cross: the JPY/EUR leg was not published for this date",
    )


def test_the_fx_job_stores_the_payload_and_every_rate(tmp_path):
    rates = [
        _rate(date(2026, 9, 15), "1.1539", "178.86", "155.004766"),
        _rate(date(2026, 9, 16), "1.1537", "178.88", "155.048973"),
    ]
    writer = RecordingWriter()
    job = FxIngestJob(store=LocalObjectStore(tmp_path), writer=writer, provider=FakeEcb(rates))

    report = job.run(last_n=6)

    assert report.ok is True
    assert report.complete_rates == 2
    assert report.incomplete_rates == 0
    assert report.stored_object_key.startswith("raw/ecb/ECB_EXR_DAILY/")
    assert len(writer.fx) == 2
    assert len(writer.objects) == 1
    assert writer.fx[-1]["derived_usd_jpy"] == Decimal("155.048973")
    assert writer.fx[-1]["rate_kind"] == "REFERENCE_RATE"


def test_an_incomplete_day_is_stored_as_incomplete_not_skipped(tmp_path):
    rates = [
        _rate(date(2026, 9, 15), "1.1539", "178.86", "155.004766"),
        _rate(date(2026, 9, 16), "1.1537", "178.88", None),
    ]
    writer = RecordingWriter()
    job = FxIngestJob(store=LocalObjectStore(tmp_path), writer=writer, provider=FakeEcb(rates))

    report = job.run(last_n=6)

    assert report.incomplete_rates == 1
    assert any("one leg" in note for note in report.notes)
    assert len(writer.fx) == 2, "the incomplete date is still a row"
    assert writer.fx[-1]["derived_usd_jpy"] is None
    assert writer.fx[-1]["notes"]


def test_a_not_modified_response_writes_nothing_and_says_so(tmp_path):
    writer = RecordingWriter()
    provider = FakeEcb([], not_modified=True)
    job = FxIngestJob(store=LocalObjectStore(tmp_path), writer=writer, provider=provider)

    report = job.run(last_n=6, if_modified_since=http_date(NOW))

    assert report.not_modified is True
    assert report.ok is True
    assert writer.fx == []
    assert writer.objects == []
    assert provider.last_if_modified_since == http_date(NOW)


def test_a_provider_failure_is_reported_not_raised(tmp_path):
    job = FxIngestJob(
        store=LocalObjectStore(tmp_path), provider=FakeEcb([], raise_with=TimeoutError("no route"))
    )
    report = job.run(last_n=6)

    assert report.ok is False
    assert "TimeoutError" in report.error


def test_the_newest_complete_cross_is_what_the_filter_gets():
    rates = [
        _rate(date(2026, 9, 15), "1.1539", "178.86", "155.004766"),
        _rate(date(2026, 9, 16), "1.1537", "178.88", None),
    ]
    observation = latest_observation(rates, observed_at=NOW, available_at=NOW)

    assert observation is not None
    assert observation.source_date == date(2026, 9, 15), "an incomplete day is not a rate"
    assert observation.rate == Decimal("155.004766")

    assert latest_observation([], observed_at=NOW, available_at=NOW) is None


def test_the_stored_row_keeps_both_legs_and_the_derivation():
    params = rate_params(
        _rate(date(2026, 9, 16), "1.1537", "178.88", "155.048973"),
        content_sha256="b" * 64, raw_object_key="raw/ecb/x.csv",
        observed_at=NOW, available_at=NOW, source_published_at=NOW - timedelta(hours=10),
        run_id=None,
    )
    assert params["eur_usd"] == Decimal("1.1537")
    assert params["eur_jpy"] == Decimal("178.88")
    assert "EXR.D.JPY.EUR.SP00.A" in params["derivation_method"]
    assert params["availability_basis"] == "OBSERVED_NOW"
    assert params["source_published_at"] < params["available_at"]


# ------------------------------------------------------------- eligibility
def test_the_filter_runs_across_a_universe_and_records_every_outcome():
    writer = RecordingWriter()
    job = PriceEligibilityJob(writer=writer, run_id="run-1")

    report = job.run(
        market_code="JP",
        as_of_date=date(2026, 1, 8),
        knowledge_cutoff=NOW,
        bars_by_symbol={
            "CHEAP": _bars("CHEAP", ["100", "200", "300"]),
            "DEAR": _bars("DEAR", ["5000", "5100", "5200"]),
            "SILENT": [],
        },
        universe_decisions={"CHEAP": "INCLUDED", "DEAR": "INCLUDED", "SILENT": "UNRESOLVED"},
    )

    assert report.summary["price_eligible"] == 1
    assert report.summary["price_above_3000"] == 1
    assert report.summary["price_missing"] == 1
    assert report.eligible_symbols == {"CHEAP"}
    assert report.written == 3, "every security gets a row, including the ones with no price"


def test_a_us_universe_without_a_rate_is_unmeasured_across_the_board():
    job = PriceEligibilityJob()
    report = job.run(
        market_code="US", as_of_date=date(2026, 1, 8), knowledge_cutoff=NOW,
        bars_by_symbol={"AAPL": _bars("AAPL", ["10", "11", "12"], market="US")}, fx=None,
    )
    assert report.summary["fx_missing"] == 1


def test_a_bar_the_cutoff_could_not_know_never_reaches_the_rule():
    future_bar = _bars("X", ["100"])[0]
    future_bar = CanonicalBar(**{**future_bar.__dict__, "available_at": NOW + timedelta(days=1)})

    report = PriceEligibilityJob().run(
        market_code="JP", as_of_date=future_bar.trade_date, knowledge_cutoff=NOW,
        bars_by_symbol={"X": [future_bar]},
    )
    assert report.results[0][1].decision is EligibilityDecision.PRICE_MISSING


# --------------------------------------------------------------- stage 1
def test_the_screening_job_produces_features_and_candidates():
    closes = [str(100 + i) for i in range(90)]
    writer = RecordingWriter()
    bars = _bars("RISER", closes)
    job = Stage1ScreeningJob(writer=writer, run_id="run-2")

    report = job.run(
        market_code="JP",
        as_of_date=bars[-1].trade_date,
        bars_by_symbol={"RISER": bars},
        observed_at=NOW,
        available_at=NOW,
        price_eligible={"RISER": True},
    )

    assert len(report.features) == 1
    assert report.features_written == 1
    assert report.features[0].warmup_satisfied is True
    assert report.summary["securities_with_features"] == 1


def test_screening_skips_what_the_price_filter_already_excluded():
    bars = _bars("DEAR", [str(5000 + i) for i in range(90)])
    report = Stage1ScreeningJob().run(
        market_code="JP", as_of_date=bars[-1].trade_date,
        bars_by_symbol={"DEAR": bars}, observed_at=NOW, available_at=NOW,
        price_eligible={"DEAR": False},
    )
    assert report.features == []

    # research legitimately wants the excluded population too
    research = Stage1ScreeningJob().run(
        market_code="JP", as_of_date=bars[-1].trade_date,
        bars_by_symbol={"DEAR": bars}, observed_at=NOW, available_at=NOW,
        price_eligible={"DEAR": False}, screen_only_eligible=False,
    )
    assert len(research.features) == 1


def test_a_security_whose_split_cannot_be_read_is_refused_and_named():
    bars = _bars("BROKEN", [str(100 + i) for i in range(40)])
    broken = CanonicalAction(
        provider_id="free", dataset_key="ECB_EXR_DAILY", market_code="JP",
        native_symbol="BROKEN", action_type=CorporateActionType.SPLIT, ex_date=bars[-1].trade_date,
    )

    report = Stage1ScreeningJob().run(
        market_code="JP", as_of_date=bars[-1].trade_date,
        bars_by_symbol={"BROKEN": bars}, actions_by_symbol={"BROKEN": [broken]},
        observed_at=NOW, available_at=NOW, price_eligible={"BROKEN": True},
    )

    assert report.features == []
    assert report.refused[0][0] == "BROKEN"
    assert "features:" in report.refused[0][1]
    assert report.summary["securities_refused"] == 1


def test_a_security_with_no_bars_before_the_as_of_date_is_refused_not_crashed():
    bars = _bars("LATE", ["100", "101"], start=date(2026, 6, 1))
    report = Stage1ScreeningJob().run(
        market_code="JP", as_of_date=date(2026, 1, 8),
        bars_by_symbol={"LATE": bars}, observed_at=NOW, available_at=NOW,
        price_eligible={"LATE": True},
    )
    assert report.features == []
    assert "series:" in report.refused[0][1]


def test_the_chain_from_prices_to_candidates():
    """The whole zero-cost daily path, end to end."""

    closes = [str(round(100 + i * 0.5, 2)) for i in range(90)]
    bars_by_symbol = {
        "CHEAP": _bars("CHEAP", closes),
        "DEAR": _bars("DEAR", [str(5000 + i) for i in range(90)]),
    }
    as_of = bars_by_symbol["CHEAP"][-1].trade_date

    eligibility = PriceEligibilityJob().run(
        market_code="JP", as_of_date=as_of, knowledge_cutoff=NOW, bars_by_symbol=bars_by_symbol
    )
    assert eligibility.eligible_symbols == {"CHEAP"}

    stage1 = Stage1ScreeningJob().run(
        market_code="JP", as_of_date=as_of, bars_by_symbol=bars_by_symbol,
        observed_at=NOW, available_at=NOW,
        price_eligible={s: s in eligibility.eligible_symbols for s in bars_by_symbol},
    )

    assert [f.native_symbol for f in stage1.features] == ["CHEAP"]
    assert stage1.summary["securities_with_features"] == 1
    # whether any route fires depends on the shape; the chain must run either way
    assert stage1.summary["candidates"] == len(stage1.candidates)


def test_the_us_chain_converts_before_it_filters():
    bars = _bars("USSTOCK", ["19", "19", "19"], market="US")
    fx = FxObservation(Decimal("155.0"), date(2026, 1, 7), YESTERDAY, YESTERDAY)

    cheap = PriceEligibilityJob().run(
        market_code="US", as_of_date=bars[-1].trade_date, knowledge_cutoff=NOW,
        bars_by_symbol={"USSTOCK": bars}, fx=fx,
    )
    assert cheap.eligible_symbols == {"USSTOCK"}

    stronger_yen = FxObservation(Decimal("170.0"), date(2026, 1, 7), YESTERDAY, YESTERDAY)
    dear = PriceEligibilityJob().run(
        market_code="US", as_of_date=bars[-1].trade_date, knowledge_cutoff=NOW,
        bars_by_symbol={"USSTOCK": bars}, fx=stronger_yen,
    )
    assert dear.eligible_symbols == set()
    assert dear.results[0][1].converted_jpy == Decimal("3230.000000")
