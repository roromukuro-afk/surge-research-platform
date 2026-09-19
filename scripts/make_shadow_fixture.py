"""Write the Phase B web shadow's fixture: a made-up cohort in the shadow's artifact format (D-279).

Everything is synthetic - 40 securities with random-walk prices, their JPX
entries, disclosure titles and TypeSafe's answers - and goes through the real
Phase B code: the scheduled prediction and outcome jobs (D-277, D-278), every
weekday from 2026-09-21 to 2026-10-27 on a fake clock, with fakes in place of
Yahoo, Yanoshin and TypeSafe. The cohort is cut to 4 business days so that it
reaches its outcomes and reports in a few seconds; one day is stopped by the
coverage guard on the way. The result is exported with surge.shadow.export into
``apps/web/fixtures/shadow``, which the web reads when SURGE_SHADOW_SOURCE is
``fixture``.

No network, no key, no database. The fixture is labelled ``synthetic-fixture``
in every commit record and the web says so on every page.

    python scripts/make_shadow_fixture.py [--out apps/web/fixtures/shadow]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "workers" / "src"), str(REPO / "workers" / "tests")]

import pytest  # noqa: E402 - the Phase B test helpers patch through pytest's MonkeyPatch

import test_evaluation_phase_b as t  # noqa: E402
from surge.analysis.jev_questions import DECISIONS  # noqa: E402
from surge.evaluation import jpx_calendar, phase_b  # noqa: E402
from surge.providers.yahoo_finance import JST  # noqa: E402
from surge.shadow.artifacts import json_bytes  # noqa: E402
from surge.shadow.export import export_cohort  # noqa: E402
from surge.storage.local import LocalObjectStore  # noqa: E402
from test_evaluation import _direct_answer_body, _tokens_within_estimate  # noqa: E402

COHORT = "synthetic-phase-b-fixture"
SEED = "synthetic-fixture-seed"
FIRST = date(2026, 9, 21)
AS_OF = datetime(2026, 10, 27, 19, 0, tzinfo=JST)
STOPPED_DAY = date(2026, 9, 28)  # three of forty securities unread: 92.5% coverage, below the 95% guard
EXPORTED_AT = datetime(2026, 10, 27, 10, 30, tzinfo=UTC)


class _VariedTransport:
    """TypeSafe's API, made up: complete answers from jev-1.13.0, varied by the request's own bytes."""

    def __init__(self) -> None:
        self.sent = 0

    def __call__(self, url, data, headers, timeout):
        self.sent += 1
        h = hashlib.sha256(data).digest()
        body = _direct_answer_body(model="jev-1.13.0", tokens=_tokens_within_estimate(data))
        options = list(DECISIONS)
        weights = [1 + h[i] % 7 for i in range(len(options))]
        weights[options.index("REJECT")] += 6  # most setups are rejected, as they should be
        total = sum(weights)
        probabilities = {k: round(w / total, 2) for k, w in zip(options, weights, strict=True)}
        probabilities[options[-1]] = round(1 - sum(v for k, v in probabilities.items() if k != options[-1]), 2)
        answers = body["answers"]
        answers["decision"].update(choice=max(probabilities, key=probabilities.get),
                                   probabilities=probabilities, confidence=round(0.25 + h[10] / 510, 2))
        answers["reaches_target"]["noul"] = round(0.04 + h[11] / 600, 2)
        answers["upside_band"].update(score=round(h[12] / 255, 2), confidence=round(0.3 + h[13] / 510, 2))
        return 200, {"x-typesafe-request-id": f"req_{self.sent}"}, json.dumps(body).encode("utf-8")


def _weekdays(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def build(out: Path) -> dict:
    t.COHORT, t.SEED = COHORT, SEED
    monkeypatch = pytest.MonkeyPatch()
    work = Path(tempfile.mkdtemp(prefix="shadow-fixture-"))
    try:
        monkeypatch.setattr(phase_b, "TARGET_BUSINESS_DAYS", 4)
        env = t._setup(work, monkeypatch, until=date(2026, 11, 30), holidays=True)
        transport = _VariedTransport()
        statuses = []
        for day in _weekdays(FIRST, AS_OF.date()):
            yahoo = (t._FlakyYahoo(env.charts, {"3001.T", "3002.T", "3003.T"}) if day == STOPPED_DAY
                     else t._FakeYahoo(env.charts))
            start = datetime(day.year, day.month, day.day, 16, 10, 5, tzinfo=JST)
            record = t._scheduled(env, start, yahoo=yahoo, transport=transport)
            statuses.append((day.isoformat(), "prediction", record["status"]))
            outcome = t._outcome_run(env, datetime(day.year, day.month, day.day, 18, 0, 3, tzinfo=JST))
            statuses.append((day.isoformat(), "outcome", outcome["status"]))
        if out.exists():
            shutil.rmtree(out)
        store = LocalObjectStore(out, store_id="fixture")
        written = export_cohort(env.root, COHORT, store, system="synthetic-fixture", exported_at=EXPORTED_AT)
        marker = json_bytes({
            "what": "a synthetic Phase B cohort for the web shadow's screens: made-up securities, prices, "
                    "disclosures and answers, run through the real Phase B code with fakes for every provider",
            "generator": "scripts/make_shadow_fixture.py",
            "cohort_id": COHORT,
            "as_of": AS_OF.isoformat(),
            "target_business_days": 4,
            "calendar_version": jpx_calendar.CALENDAR_VERSION,
            "not_real_data": True,
        })
        # At the root, so a directory names the cohort it holds; beside the cohort, so the label travels with it.
        store.put_immutable("fixture.json", marker, "application/json")
        store.put_immutable(f"surge/phase-b/{COHORT}/fixture.json", marker, "application/json")
        return {"statuses": statuses, "written": written, "sent": transport.sent}
    finally:
        monkeypatch.undo()
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "apps" / "web" / "fixtures" / "shadow")
    args = parser.parse_args(argv)
    result = build(args.out)
    for day, job, status in result["statuses"]:
        print(f"{day} {job:<10} {status}")
    days = result["written"]["days"]
    print(f"sent {result['sent']} made-up requests; days {[d['s0'] for d in days if d.get('exported')]}; "
          f"reports {result['written']['reports']}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
