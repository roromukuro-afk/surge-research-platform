"""Jev evaluation Phase B: the prospective shadow cohort (D-272 as decided 2026-09-19, D-275).

A **cohort** is created once, before its first day, and records everything the
evaluation may not change while it runs (``frozen_protocol``): Canonical and
the addenda, the question schema and the state Jev is given, the universe and
the screener (Routes A-H, route-1.0.0 on features-1.0.0), the daily selection
(``surge.evaluation.selection``), the outcome definition and how Jev's answers
are read. A day whose code differs from its cohort in any of these is refused;
a change means a new cohort. Everything else (fetching, pacing, budgets,
reporting) may be fixed, and each day records the code it ran with.

**A day** (S0 >= 2026-09-24, a TSE session) may start once every S0 bar is
final - the session end plus twice Yahoo's 20-minute delay, so that even a
thin security's last trade is its close (D-262) - and must be sent before S1
can open: 09:00 JST on the next weekday (a holiday only moves the real S1
later). Its requests go to TypeSafe's own API with the model pinned to
``jev-1.13.0``; an answer from any other version stops the whole cohort, and
the Gateway, whose answers carry no version, cannot join it.

**Money.** Each day has its own hard budget; the cohort as a whole may spend
at most $2.50 on TypeSafe direct (recorded costs, estimates where no cost is
known, plus the next request's estimate); the account's credit comes from the
latest console snapshot (``surge.evaluation.cost``). Nothing here buys credit.

Nothing here writes to a database. Every row is ``teacher_admissible = false``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from surge.evaluation.cost import BudgetExceeded, charged_usd
from surge.evaluation.method import REPO_ROOT
from surge.evaluation.outcome import OUTCOME_DEFINITION, OUTCOME_VERSION
from surge.evaluation.population import PRICE_LIMIT_JPY, SCREENER_DEFINITION
from surge.evaluation.report import DECISION_INTERPRETATION, prediction_row
from surge.evaluation.selection import (
    ANONYMIZED_PER_DAY,
    CONTROL_PER_DAY,
    DRIFT_PER_DAY,
    MAX_REQUESTS_PER_DAY,
    PHASE_B_START,
    PRIMARY_PER_DAY,
    SELECTION_VERSION,
    SUBGROUP_DEFINITION,
)
from surge.evaluation.state import STATE_VERSION, question_schema_hash
from surge.evaluation.store import RunStore
from surge.features.engine import FEATURE_VERSION
from surge.providers.yahoo_finance import JST, TSE_PUBLICATION_DELAY, tse_session_end
from surge.routes.engine import ROUTE_VERSION

#: TypeSafe's Models documentation accepts a version id as ``model``; a request
#: pinned to it on 2026-09-19 was answered by it (D-272 #7).
PINNED_MODEL = "jev-1.13.0"
GLOBAL_HARD_CAP_USD = Decimal("2.50")
#: A day of 92 requests is estimated at about $0.10 (spent: about $0.087).
DAILY_BUDGET_USD = Decimal("0.12")
TARGET_BUSINESS_DAYS = 25
#: How far back each day's history reaches: the 75-bar warm-up and the 60-session
#: windows, with the smoothed indicators well past their start.
HISTORY_LOOKBACK_DAYS = 400
#: A day whose universe could not be read almost whole is not drawn from.
MIN_UNIVERSE_COVERAGE = 0.95
OPEN_JST = time(9, 0)
#: What a cohort freezes, besides the values in ``frozen_protocol``.
FROZEN_FILES = [
    "workers/src/surge/analysis/jev_questions.py",
    "workers/src/surge/evaluation/state.py",
    "workers/src/surge/evaluation/universe.py",
    "workers/src/surge/evaluation/population.py",
    "workers/src/surge/evaluation/selection.py",
    "workers/src/surge/evaluation/outcome.py",
    "workers/src/surge/market/series.py",
    "workers/src/surge/features/engine.py",
    "workers/src/surge/features/indicators.py",
    "workers/src/surge/routes/engine.py",
]
NOT_INTRODUCED = [
    "a probability threshold",
    "a confidence threshold",
    "averaging repeated answers (3-run or any other)",
    "a code-side override of Jev's decision",
    "Jev in production predictions",
    "registration in the teacher dataset",
]
PREREGISTERED = {
    "route_d_subgroups": SUBGROUP_DEFINITION,
    "route_d_handling": "Route D's thresholds and definition are unchanged; D-only candidates are drawn like any "
                        "other (no exclusion, no down-weighting); changes are considered only after Phase B",
    "subgroup_metrics": [
        "reaches_target calibration (10 bins, ECE) and Brier, for +20% on the high and on the close",
        "+20% hit rate, on the high and on the close",
        "decision distribution",
        "upside score (mean, median)",
        "maximum future return (max upside on the high and on the close; mean, median)",
        "maximum drawdown (on the low and on the close; mean, median)",
    ],
    "cohorts_pooled": False,
    "primary_metrics": "Brier and Brier skill, calibration, PR-AUC, hit rate by decision, score against future "
                       "return (Primary only); Control is a benchmark with hit rates only",
}
_COHORT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


class PhaseBError(RuntimeError):
    pass


# ----------------------------------------------------------------- when a day may run


def close_confirmed_at(s0: date) -> datetime:
    """From when every S0 bar is final (D-262): the session end plus twice the feed's delay."""

    return tse_session_end(s0) + 2 * TSE_PUBLICATION_DELAY


def send_deadline(s0: date) -> datetime:
    """The earliest S1 can open: 09:00 JST on the next weekday. Nothing of the day is sent from then on."""

    day = s0 + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, OPEN_JST, tzinfo=JST).astimezone(UTC)


def schedule(now: datetime) -> dict:
    """Which S0's window is open at ``now`` - after its close is final and before S1 can open - or the next one."""

    today = now.astimezone(JST).date()
    for back in range(0, 5):
        day = today - timedelta(days=back)
        if day >= PHASE_B_START and day.weekday() < 5 and close_confirmed_at(day) <= now < send_deadline(day):
            return {"s0": day.isoformat(), "window_open": True, "opens_at": close_confirmed_at(day).isoformat(),
                    "closes_at": send_deadline(day).isoformat()}
    day = max(today, PHASE_B_START)
    while day.weekday() >= 5 or close_confirmed_at(day) <= now:
        day += timedelta(days=1)
    return {"s0": None, "window_open": False, "next_s0": day.isoformat(),
            "opens_at": close_confirmed_at(day).isoformat(), "closes_at": send_deadline(day).isoformat()}


# ----------------------------------------------------------------- what a cohort freezes


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def frozen_protocol(method, *, evaluation_version: str, repo_root: Path = REPO_ROOT) -> dict:
    """Everything a cohort may not change, as values and file hashes."""

    return {
        "evaluation_version": evaluation_version,
        "method": method.manifest_entry(),
        "question_schema_hash": question_schema_hash(),
        "state_version": STATE_VERSION,
        "screener": {"definition": SCREENER_DEFINITION, "route_version": ROUTE_VERSION,
                     "feature_version": FEATURE_VERSION, "price_limit_jpy": str(PRICE_LIMIT_JPY),
                     "history_lookback_days": HISTORY_LOOKBACK_DAYS},
        "selection": {"version": SELECTION_VERSION, "prospective_start": PHASE_B_START.isoformat(),
                      "per_day": per_day()},
        "outcome": {"version": OUTCOME_VERSION, "definition": OUTCOME_DEFINITION},
        "decision_interpretation": {
            **DECISION_INTERPRETATION,
            "prediction_row_sha256": hashlib.sha256(inspect.getsource(prediction_row).encode("utf-8")).hexdigest(),
        },
        "files_sha256": {relative: _sha256(repo_root / relative) for relative in FROZEN_FILES},
    }


def fingerprint(frozen: dict) -> str:
    return hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def frozen_differences(recorded: dict, current: dict) -> list[str]:
    """Which frozen parts differ, named; empty when the cohort's protocol still holds."""

    differences = []
    for key in sorted(set(recorded) | set(current)):
        if key == "files_sha256":
            files = sorted(f for f in set(recorded.get(key, {})) | set(current.get(key, {}))
                           if recorded.get(key, {}).get(f) != current.get(key, {}).get(f))
            differences += [f"file {f}" for f in files]
        elif recorded.get(key) != current.get(key):
            differences.append(key)
    return differences


def per_day() -> dict:
    return {"primary": PRIMARY_PER_DAY, "control": CONTROL_PER_DAY, "anonymized": ANONYMIZED_PER_DAY,
            "drift": DRIFT_PER_DAY, "max_requests": MAX_REQUESTS_PER_DAY}


# ----------------------------------------------------------------- where a cohort lives


def check_cohort_id(cohort_id: str) -> str:
    if not _COHORT_ID.match(cohort_id or ""):
        raise PhaseBError(f"{cohort_id!r}: a cohort id is 3-63 lowercase letters, digits and hyphens")
    return cohort_id


def cohort_store(root: Path, cohort_id: str) -> RunStore:
    return RunStore(root, check_cohort_id(cohort_id))


def day_store(root: Path, cohort_id: str, s0: date) -> RunStore:
    return RunStore(root, f"{check_cohort_id(cohort_id)}/days/{s0.isoformat()}")


def day_dates(root: Path, cohort_id: str) -> list[date]:
    days = cohort_store(root, cohort_id).path / "days"
    return sorted(date.fromisoformat(p.name) for p in days.iterdir() if p.is_dir()) if days.exists() else []


def completed_days(root: Path, cohort_id: str) -> list[date]:
    """The days sent in full (``stage-run.json``): the ones that count towards the 25-day target."""

    return [day for day in day_dates(root, cohort_id) if day_store(root, cohort_id, day).exists("stage-run.json")]


def day_spend(store: RunStore) -> Decimal:
    """What one day's response records count against the cohort cap."""

    if not (store.path / "responses").exists():
        return Decimal(0)
    estimates = ({f"{r['sample_id']}.{r['variant']}": Decimal(r["estimated_usd"])
                  for r in store.read_jsonl("requests.jsonl")} if store.exists("requests.jsonl") else {})
    total = Decimal(0)
    for path in sorted((store.path / "responses").glob("*.json")):
        if ".raw" in path.name:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        total += charged_usd(record, estimates.get(record.get("label")))
    return total


def cohort_spend(root: Path, cohort_id: str, *, exclude: date | None = None) -> Decimal:
    """The cohort's TypeSafe direct spend so far, over every day but ``exclude``."""

    return sum((day_spend(day_store(root, cohort_id, day)) for day in day_dates(root, cohort_id) if day != exclude),
               Decimal(0))


def check_cap(spent: Decimal, estimate: Decimal, cap: Decimal = GLOBAL_HARD_CAP_USD) -> None:
    """Before every request of a Phase B day: what the cohort has spent, plus this request, within the cap."""

    if spent + estimate > cap:
        raise BudgetExceeded(f"${spent} spent by the cohort + ${estimate:.6f} estimated would pass Phase B's "
                             f"${cap} cap")


def cohort_stops(root: Path, cohort_id: str) -> list[dict]:
    store = cohort_store(root, cohort_id)
    stops, n = [], 1
    while store.exists(f"cohort-stopped-{n}.json"):
        stops.append(store.read_json(f"cohort-stopped-{n}.json"))
        n += 1
    return stops


def stop_cohort(root: Path, cohort_id: str, *, s0: date, reason: str, detail: str) -> str:
    """Stop the whole cohort (a served version other than the pinned one): no day runs after this."""

    store = cohort_store(root, cohort_id)
    n = len(cohort_stops(root, cohort_id)) + 1
    store.write_json(f"cohort-stopped-{n}.json", {"stopped_at": datetime.now(UTC).isoformat(), "s0": s0.isoformat(),
                                                   "reason": reason, "detail": detail[:400]})
    return f"cohort-stopped-{n}.json"


__all__ = ["DAILY_BUDGET_USD", "FROZEN_FILES", "GLOBAL_HARD_CAP_USD", "HISTORY_LOOKBACK_DAYS",
           "MIN_UNIVERSE_COVERAGE", "NOT_INTRODUCED", "PINNED_MODEL", "PREREGISTERED", "TARGET_BUSINESS_DAYS",
           "PhaseBError", "check_cap", "check_cohort_id", "close_confirmed_at", "cohort_spend", "cohort_stops",
           "cohort_store", "completed_days", "day_dates", "day_spend", "day_store", "fingerprint", "frozen_differences",
           "frozen_protocol", "per_day", "schedule", "send_deadline", "stop_cohort"]
