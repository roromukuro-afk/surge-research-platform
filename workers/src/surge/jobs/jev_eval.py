"""`python -m surge.jobs.jev_eval` - the Jev evaluation (docs/specs/jev-evaluation-design.md, D-270, D-272).

Each stage writes write-once files under ``<root>/evaluation/jev/<run_id>/``
(root: ``SURGE_EVALUATION_ROOT``, default ``~/.surge`` - outside the repository,
because the files hold market data and answers about real securities).

Phase A, the retrospective pilot (complete):

    plan            --run-id ID [--seed N] [--symbols N]    the population (reads JPX, Yahoo)
    build           --run-id ID                              states and the exact request bodies (reads Yanoshin)
    freeze-outcomes --run-id ID                              outcomes, fixed before any answer exists
    preflight       --run-id ID --budget-usd X --max-requests N [--runner R]
                                                             leakage checks, the budget, the credit balance
    run             --run-id ID --budget-usd X --max-requests N [--runner R]
                                                             the requests, inside the hard budget
    report          --run-id ID                              report.json and report.md

Phase B, the prospective shadow cohort (``surge.evaluation.phase_b``, D-272):

    phase-b-init    --cohort-id C --experiment-seed S        freeze the protocol; reads and sends nothing
    record-credit   --balance-usd X --confirmed-at T --expires-on D
                                                             one reading of TypeSafe's console balance
    phase-b-day     --cohort-id C --s0 D|auto [--send]       one business day: plan-day, build, preflight;
                                                             the requests only with --send
    plan-day        --cohort-id C --s0 D                     the day's universe, screening and sample
    build / preflight / run --run-id C/days/D                as in Phase A, for one day
    freeze-outcomes --run-id C/days/D                        after T+20, from prices read then
    phase-b-status  --cohort-id C
    phase-b-report  --cohort-id C                            every day whose outcomes are frozen

Only ``run`` (and ``phase-b-day --send``) sends model requests. ``preflight``
reads the Gateway's credit balance through the runner, or, for TypeSafe
direct, the latest console snapshot less the spend recorded since (TypeSafe
has no balance API), and sends nothing. The provider is the manifest's: the
Gateway for Phase A; TypeSafe direct pinned to ``jev-1.13.0`` for Phase B,
whose cohort the Gateway (no version in its answers) cannot join.

Nothing here writes to a database; every row is ``teacher_admissible = false``.

``R`` is ``ops/jev-gateway-runner/runner.mjs``; it needs AI_GATEWAY_API_KEY in
its environment, and TypeSafe direct needs TYPESAFE_API_KEY (both through
Invoke-WithSurgeSecrets.ps1).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import random
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from surge.analysis.jev_questions import GATEWAY_MODEL, MODEL, questions, to_gateway
from surge.evaluation import phase_b
from surge.evaluation.cost import (
    CONTEXT_TOKENS,
    Budget,
    BudgetExceeded,
    Ledger,
    direct_credit_state,
    estimate_usd,
    estimated_jev_tokens,
    plan_problems,
    read_credits,
    record_credit_snapshot,
)
from surge.evaluation.leakage import check_prospective_sample, check_sample
from surge.evaluation.method import REPO_ROOT, MethodError, load_method
from surge.evaluation.outcome import HORIZON, OUTCOME_DEFINITION, OUTCOME_VERSION, compute_outcome
from surge.evaluation.pacing import Pacer, pacer_for
from surge.evaluation.population import (
    MIN_SPACING_SESSIONS,
    SCREENER_DEFINITION,
    Sample,
    Screen,
    choose_s0_dates,
    draw_population,
    reduce_population,
    screen,
)
from surge.evaluation.prices import (
    DATASET_KEY,
    TURNOVER_BASIS,
    History,
    PriceHistoryError,
    fetch_chart,
    history_from_chart,
    sessions_from_counts,
    trading_sessions,
)
from surge.evaluation.providers import (
    PROVIDER_NAMES,
    SERVED_PROVIDER,
    SERVED_VERSION,
    TYPESAFE_DIRECT,
    VERCEL_GATEWAY,
    gateway_http_facts,
    provider_for,
    record_cost,
)
from surge.evaluation.report import (
    REPORT_VERSION,
    VARIANTS,
    build_report,
    paired_rows,
    prediction_row,
    render_markdown,
)
from surge.evaluation.selection import (
    ANONYMIZED_PER_DAY,
    CONTROL_PER_DAY,
    DRIFT_PER_DAY,
    MAX_REQUESTS_PER_DAY,
    PHASE_B_START,
    PRIMARY_PER_DAY,
    SELECTION_VERSION,
    SelectionError,
    check_s0,
    select_day,
    selection_signature,
)
from surge.evaluation.state import (
    STATE_VERSION,
    build_state,
    disclosure_coverage,
    question_schema_hash,
    request_body,
    select_disclosures,
)
from surge.evaluation.store import RunStore, StoreError, default_root
from surge.evaluation.universe import ListedIssue
from surge.features.engine import FEATURE_VERSION
from surge.routes.engine import ROUTE_VERSION

#: 1.1.0 adds Phase B (D-272); Phase A's runs were made with 1.0.0.
EVALUATION_VERSION = "jev-eval-1.1.0"
RUNNER = REPO_ROOT / "ops" / "jev-gateway-runner" / "runner.mjs"
#: Who answers, by either route: TypeSafe's (D-269, D-275).
EXPECTED_PROVIDER = SERVED_PROVIDER
#: A 429 from a provider that says how long to wait (TypeSafe direct): the wait
#: is honoured, up to these limits; past them the run stops (D-275). Without a
#: stated wait, the fallback backoff doubles from FALLBACK_BACKOFF_SECONDS.
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_WAIT_SECONDS = 1800.0
FALLBACK_BACKOFF_SECONDS = 60.0
#: Seven months before the first S0: the feature warm-up (75 sessions) and the
#: 60-session highs need that much behind the earliest S0.
HISTORY_START = date(2024, 6, 1)
PHASES = {
    # The official population is drawn as designed (75 / 25, D-270); Phase A
    # then evaluates a seeded subset of it (D-274): end-to-end pipeline
    # verification needs 24 requests, not 115, under the free tier's limit.
    "A": {"s0_start": date(2025, 1, 1), "s0_end": date(2026, 6, 30),
          "primary": 75, "control": 25, "anonymized": 10, "drift": 5,
          "evaluated": {"primary": 15, "control": 5, "anonymized": 2, "drift": 2},
          # Phase A ran through the Gateway; Phase B's primary is TypeSafe direct (D-275).
          "provider": VERCEL_GATEWAY},
}
PHASE_A_LIMITATIONS = [
    "screener = Routes A-H (route-1.0.0 on features-1.0.0) replayed point-in-time; material routes M1-M6 and "
    "Stage 2 are not replayed",
    "universe = the current JPX workbook: delisted securities are absent (survivorship); segment, sector and size "
    "are current values, not point-in-time",
    "a seeded subset of the universe is sampled, not every security",
    "turnover is approximated as the as-traded close x volume (Yahoo has none); route F reads it",
    "disclosure titles come from Yanoshin's per-code index (newest rows only); a window the index does not reach "
    "is recorded as INCOMPLETE coverage",
    "Phase A results are not used for adoption: Jev may have seen these outcomes in training",
]
PHASE_B_LIMITATIONS = [
    "screener = Routes A-H (route-1.0.0 on features-1.0.0) on the day's Yahoo history; material routes M1-M6 and "
    "Stage 2 are not part of it",
    "universe = the JPX workbook as published on S0's evening; a security it does not list is not screened, and a "
    "history Yahoo would not return is recorded as a failure",
    "turnover is approximated as the as-traded close x volume (Yahoo has none); route F reads it",
    "disclosure titles come from Yanoshin's per-code index read when the day is built; a window the index does not "
    "reach is recorded as INCOMPLETE coverage",
]
#: Everything that shapes an input, an outcome or a number. Their hashes are the
#: input-building code version, whether or not the tree is committed.
CODE_FILES = [
    "workers/src/surge/evaluation/__init__.py",
    "workers/src/surge/evaluation/cost.py",
    "workers/src/surge/evaluation/leakage.py",
    "workers/src/surge/evaluation/method.py",
    "workers/src/surge/evaluation/outcome.py",
    "workers/src/surge/evaluation/pacing.py",
    "workers/src/surge/evaluation/phase_b.py",
    "workers/src/surge/evaluation/population.py",
    "workers/src/surge/evaluation/prices.py",
    "workers/src/surge/evaluation/providers.py",
    "workers/src/surge/evaluation/report.py",
    "workers/src/surge/evaluation/selection.py",
    "workers/src/surge/evaluation/state.py",
    "workers/src/surge/evaluation/store.py",
    "workers/src/surge/evaluation/universe.py",
    "workers/src/surge/jobs/jev_eval.py",
    "workers/src/surge/jobs/jev_smoke.py",
    "workers/src/surge/analysis/jev_questions.py",
    "workers/src/surge/providers/yahoo_finance.py",
    "workers/src/surge/providers/jpx_listed.py",
    "workers/src/surge/news/sources/yanoshin_tdnet.py",
    "workers/src/surge/market/series.py",
    "workers/src/surge/market/models.py",
    "workers/src/surge/features/engine.py",
    "workers/src/surge/features/indicators.py",
    "workers/src/surge/routes/engine.py",
    "ops/jev-gateway-runner/runner.mjs",
    "ops/jev-gateway-runner/describe-error.mjs",
    "ops/jev-gateway-runner/package.json",
    "ops/jev-gateway-runner/package-lock.json",
]
YAHOO_PAUSE_SECONDS = 0.3
PRICE_ARCHIVE = "inputs/prices.jsonl.gz"
OUTCOME_PRICE_ARCHIVE = "outcome-inputs/prices.jsonl.gz"
#: How far before S0 the outcome's price history is read again after T+20.
OUTCOME_LOOKBACK_DAYS = 30

Clock = Callable[[], datetime]


class EvaluationError(RuntimeError):
    pass


class SendWindowClosed(EvaluationError):
    """S1 may have opened: nothing more of the day is sent (Phase B is prospective)."""


# ----------------------------------------------------------------- helpers


def _now(clock: Clock | None) -> datetime:
    return (clock or (lambda: datetime.now(UTC)))()


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
                                   check=False)
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def code_version() -> dict:
    files = {}
    for relative in CODE_FILES:
        path = REPO_ROOT / relative
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    combined = hashlib.sha256("".join(f"{k}\0{v}\n" for k, v in sorted(files.items())).encode()).hexdigest()
    status = _git("status", "--porcelain", "--", *CODE_FILES)
    return {
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty_in_these_files": None if status is None else bool(status),
        "code_sha256": combined,
        "files_sha256": files,
        "evaluation_version": EVALUATION_VERSION,
        "state_version": STATE_VERSION,
        "feature_version": FEATURE_VERSION,
        "route_version": ROUTE_VERSION,
        "outcome_version": OUTCOME_VERSION,
        "report_version": REPORT_VERSION,
        "selection_version": SELECTION_VERSION,
        "price_dataset": DATASET_KEY,
        "turnover_basis": TURNOVER_BASIS,
    }


def _runner_metadata() -> dict:
    package = json.loads((RUNNER.parent / "package.json").read_text(encoding="utf-8"))
    return {"ai_sdk": package["dependencies"]["ai"], "runner_sha256": hashlib.sha256(RUNNER.read_bytes()).hexdigest()}


def _model_metadata(provider: str, *, pinned: str | None = None) -> dict:
    from surge.analysis.jev_questions import ENDPOINT

    if provider == TYPESAFE_DIRECT and pinned:
        return {
            "requested_model": pinned,
            "endpoint": ENDPOINT,
            "upstream": "typesafe-ai (TypeSafe System One), called directly",
            "pinning": "a version id as `model` (docs.typesafe.ai/models.md); on 2026-09-19 a request pinned to "
                       "jev-1.13.0 was answered by jev-1.13.0",
            "served": f"every answer's `model` must be {pinned}; any other stops the whole cohort",
            "fallback": None,
        }
    if provider == TYPESAFE_DIRECT:
        return {
            "requested_model": MODEL,
            "endpoint": ENDPOINT,
            "upstream": "typesafe-ai (TypeSafe System One), called directly",
            "served": "the served version (e.g. jev-1.13.0) is recorded per request as `model` and pinned for the "
                      "run: a change of version stops it",
            "fallback": {"provider": VERCEL_GATEWAY, "requested_model": GATEWAY_MODEL, **_runner_metadata()},
        }
    return {
        "requested_model": GATEWAY_MODEL,
        "upstream": "typesafe-ai (TypeSafe System One)",
        "typesafe_model_alias": MODEL,
        "served": "recorded per request (served_model, resolved_provider, generation_id in predictions.*) "
                  "and summarized in report.json; the Gateway id carries no version",
        **_runner_metadata(),
    }


def _provider_of(manifest: dict, override: str | None) -> str:
    """The run's provider: the manifest's, unless the fallback is chosen explicitly."""

    name = override or manifest.get("provider") or VERCEL_GATEWAY
    if name not in PROVIDER_NAMES:
        raise EvaluationError(f"unknown provider {name!r}; one of {PROVIDER_NAMES}")
    return name


def _credits(provider: str, store: RunStore, *, runner: Path | None, now: datetime | None = None) -> dict | None:
    if provider == TYPESAFE_DIRECT:
        return direct_credit_state(store.root, now=now)
    if runner is None:
        return None
    return read_credits(runner, store.path / _next_name(store, "credits"))


def _stage(store: RunStore, name: str, payload: dict, files: dict[str, str]) -> None:
    store.write_json(f"stage-{name}.json", {"stage": name, "finished_at": datetime.now(UTC).isoformat(),
                                             "files_sha256": files, **payload})


def _verify_stage(store: RunStore, name: str) -> dict:
    if not store.exists(f"stage-{name}.json"):
        raise EvaluationError(f"stage {name!r} has not been completed for {store.run_id}")
    stage = store.read_json(f"stage-{name}.json")
    for file, sha in stage["files_sha256"].items():
        store.verify(file, sha)
    return stage


def _load_samples(store: RunStore) -> list[Sample]:
    return [
        Sample(**{**row, "s0": date.fromisoformat(row["s0"]), "s0_close_as_traded": Decimal(row["s0_close_as_traded"])})
        for row in store.read_jsonl("population.jsonl")
    ]


def _chart_line(chart: dict) -> bytes:
    return (json.dumps(chart, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _read_archive(store: RunStore, name: str, codes: set[str] | None = None) -> dict[str, History]:
    histories = {}
    with gzip.open(store.path / name, "rt", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            if codes is None or payload["code"] in codes:
                histories[payload["code"]] = history_from_chart(
                    payload["code"], payload["symbol"], payload["result"],
                    fetched_at=datetime.fromisoformat(payload["fetched_at"]))
    return histories


def _load_histories(store: RunStore, codes: set[str] | None = None) -> dict[str, History]:
    """The histories as read at plan time: Phase B keeps them in one archive, Phase A one file each."""

    if store.exists(PRICE_ARCHIVE):
        return _read_archive(store, PRICE_ARCHIVE, codes)
    histories = {}
    for path in sorted((store.path / "inputs" / "prices").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if codes is None or payload["code"] in codes:
            histories[payload["code"]] = history_from_chart(
                payload["code"], payload["symbol"], payload["result"],
                fetched_at=datetime.fromisoformat(payload["fetched_at"]),
            )
    return histories


def _sessions(store: RunStore) -> list[date]:
    return [date.fromisoformat(d) for d in store.read_json("sessions.json")["sessions"]]


def _o200k(text: str) -> int:
    from surge.analysis.tokenizer import exact_tokens_or_none

    tokens = exact_tokens_or_none(text, encoding="o200k_harmony")
    if tokens is None:
        raise EvaluationError("the o200k tokenizer is unavailable; the budget cannot be estimated without it")
    return tokens


def _method_matches(manifest: dict):
    method = load_method()
    if method.manifest_entry() != manifest["method"]:
        raise EvaluationError("Canonical or an addendum changed since the plan; start a new run")
    return method


def _code_matches(manifest: dict) -> None:
    """Every stage of a run is built by the code the manifest names, or the manifest would be wrong."""

    if code_version()["code_sha256"] != manifest["input_building_code"]["code_sha256"]:
        raise EvaluationError("the input-building code changed since the plan; plan a new run")


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fetch_one(yahoo, code: str, start: date, now: datetime, sleep) -> tuple[dict | None, History | None, dict | None]:
    """One security's Yahoo chart from ``start`` to ``now`` and its as-traded history, or why not.

    A failure is this security's, recorded, never the whole read's: Yahoo's
    refusals and the transport's own errors (a timeout, a reset connection)
    are tried once more on a new session. A programming error still raises.
    """

    from surge.providers.yahoo_finance import YahooError, tse_symbol

    symbol = tse_symbol(code)
    for attempt in (1, 2):
        try:
            result = fetch_chart(symbol, start, session=yahoo, now=now)
            history = history_from_chart(code, symbol, result, fetched_at=now)
            return {"code": code, "symbol": symbol, "fetched_at": now.isoformat(), "result": result}, history, None
        except PriceHistoryError as exc:
            return None, None, {"code": code, "error": str(exc)[:200]}
        except (TypeError, AttributeError, NameError):
            raise
        except Exception as exc:  # noqa: BLE001 - YahooError and a third-party transport's errors, at the boundary
            if attempt == 1:
                # A lapsed cookie or crumb fails every later call too; start a new session once.
                yahoo.reset()
                sleep(2)
                continue
            error = str(exc) if isinstance(exc, YahooError) else f"{type(exc).__name__}: {exc}"
            return None, None, {"code": code, "error": error[:200]}
    return None, None, {"code": code, "error": "unreachable"}


# ----------------------------------------------------------------- plan (Phase A)


def plan(store: RunStore, *, phase: str = "A", seed: int, symbols: int, per_month: int = 2,
         now: datetime | None = None, yahoo=None, fetch_issues=None, sleep=time.sleep) -> dict:
    """Choose the securities, read their history, replay the screener at each S0, draw the samples.

    Everything is computed before anything is written, so a short population
    leaves no half-written run behind.
    """

    from surge.evaluation.universe import fetch_listed_issues
    from surge.providers.yahoo_finance import YahooSession

    if store.exists("manifest.json"):
        raise StoreError(f"run {store.run_id} has already been planned")
    if phase not in PHASES:
        raise EvaluationError(f"only Phase {sorted(PHASES)} can be planned here; a Phase B day is plan-day")
    params = PHASES[phase]
    provider = params.get("provider", VERCEL_GATEWAY)
    started = now or datetime.now(UTC)
    method = load_method()

    issues, workbook_sha256 = (fetch_issues or fetch_listed_issues)()
    rng = random.Random(seed)
    pool = sorted(issues, key=lambda i: i.code)
    chosen = sorted(rng.sample(pool, min(symbols, len(pool))), key=lambda i: i.code)

    yahoo = yahoo or YahooSession()
    charts, histories, failures = {}, {}, []
    for count, issue in enumerate(chosen, start=1):
        if count % 50 == 0:
            _progress(f"prices: {count}/{len(chosen)} ({len(failures)} failed)")
        chart, history, failure = _fetch_one(yahoo, issue.code, HISTORY_START, started, sleep)
        if failure is None:
            charts[issue.code], histories[issue.code] = chart, history
        else:
            failures.append(failure)
        sleep(YAHOO_PAUSE_SECONDS)
    if not histories:
        raise EvaluationError("no price history could be read")

    sessions = trading_sessions(list(histories.values()))
    s0_dates = choose_s0_dates(sessions, params["s0_start"], params["s0_end"], per_month, rng)
    last_s0_allowed = sessions[-1 - 20] if len(sessions) > 20 else None
    s0_dates = [d for d in s0_dates if last_s0_allowed is not None and d <= last_s0_allowed]

    screens_by_date, screening_rows = {}, []
    for count, day in enumerate(s0_dates, start=1):
        _progress(f"screening: S0 {count}/{len(s0_dates)} ({day})")
        slim = []
        for code in sorted(histories):
            verdict = screen(histories[code], day)
            slim.append(replace(verdict, series=None, features=None))
            screening_rows.append(_screening_row(verdict))
        screens_by_date[day] = slim

    samples = draw_population(
        screens_by_date, {i.code: i for i in chosen}, sessions, primary=params["primary"],
        control=params["control"], anonymized=params["anonymized"], drift=params["drift"], seed=seed,
    )
    official = {c: sum(s.cohort == c for s in samples) for c in ("PRIMARY", "CONTROL")}
    if official["PRIMARY"] < params["primary"] or official["CONTROL"] < params["control"]:
        raise EvaluationError(f"the population is short: {official} against {params['primary']} / "
                              f"{params['control']}; plan again with more symbols")
    full_population = samples
    reduction_seed = f"{seed}/phase-{phase.lower()}-evaluated"
    if "evaluated" in params:
        samples = reduce_population(full_population, seed=reduction_seed, **params["evaluated"])
    counts = {c: sum(s.cohort == c for s in samples) for c in ("PRIMARY", "CONTROL")}

    files: dict[str, str] = {}
    for code, chart in sorted(charts.items()):
        files[f"inputs/prices/{code}.json"] = store.write_json(f"inputs/prices/{code}.json", chart)
    files["inputs/universe.json"] = store.write_json("inputs/universe.json", {
        "workbook_sha256": workbook_sha256, "domestic_common_stock": len(issues), "seed": seed,
        "chosen": [vars(i) for i in chosen], "failures": failures,
    })
    files["sessions.json"] = store.write_json("sessions.json", {
        "rule": "a date on which at least 30% of the sampled securities have a bar (D-142: no guessed calendar)",
        "sessions": [d.isoformat() for d in sessions],
    })
    files["screening.parquet"] = store.write_parquet("screening.parquet", screening_rows)
    if samples is not full_population:
        # The official population the evaluated subset was drawn from, kept so
        # the reduction can be checked against it.
        files["population_full.jsonl"] = store.write_jsonl("population_full.jsonl",
                                                           [s.row() for s in full_population])
    rows = [s.row() for s in samples]
    files["population.jsonl"] = store.write_jsonl("population.jsonl", rows)
    files["population.parquet"] = store.write_parquet("population.parquet", rows)

    manifest = {
        "run_id": store.run_id,
        "evaluation_version": EVALUATION_VERSION,
        "phase": phase,
        "started_at": started.isoformat(),
        "model": GATEWAY_MODEL if provider == VERCEL_GATEWAY else MODEL,
        "provider": provider,
        "fallback_provider": VERCEL_GATEWAY if provider == TYPESAFE_DIRECT else None,
        "model_version_metadata": _model_metadata(provider),
        "question_schema_hash": question_schema_hash(),
        "method": method.manifest_entry(),
        "input_building_code": code_version(),
        "population_definition": {
            "market": "JP",
            "universe": "JPX listed issues workbook (current), domestic common stock in PRIME / STANDARD / GROWTH",
            "workbook_sha256": workbook_sha256,
            "symbol_subset": {"seed": seed, "size": len(chosen), "history_read": len(histories),
                              "history_failed": len(failures)},
            "history_start": HISTORY_START.isoformat(),
            "s0_window": [params["s0_start"].isoformat(), params["s0_end"].isoformat()],
            "s0_dates_per_month": per_month,
            "s0_dates": [d.isoformat() for d in s0_dates],
            "screener": SCREENER_DEFINITION,
            "eligible": "a bar on S0 and the S0 close as traded <= 3,000 yen",
            "primary": "eligible and passing the screener at S0",
            "control": "eligible and not passing on the same S0, matched to a Primary sample on price band, then "
                       "liquidity band (quartile of 20-day turnover among that S0's eligible)",
            "counts": {"primary": params["primary"], "control": params["control"],
                       "anonymized": params["anonymized"], "drift": params["drift"]},
            "evaluated": None if samples is full_population else {
                **params["evaluated"],
                "rule": "a seeded subset of the official population, each cohort sampled apart by sample id only "
                        "(no outcome, price or answer is read); anonymized and drift drawn again, disjoint, from "
                        "the evaluated samples (D-274)",
                "seed": reduction_seed,
                "official_population_file": "population_full.jsonl",
            },
            "min_spacing_sessions": MIN_SPACING_SESSIONS,
            "seed": seed,
            "cohorts_pooled": False,
            "limitations": PHASE_A_LIMITATIONS if phase == "A" else [],
        },
        "outcome_definition": OUTCOME_DEFINITION,
        "storage": {"database_writes": "none", "teacher_admissible": False},
    }
    files["manifest.json"] = store.write_json("manifest.json", manifest)
    summary = {
        "symbols": len(chosen), "histories": len(histories), "history_failures": len(failures),
        "sessions": len(sessions), "s0_dates": len(s0_dates), "screens": len(screening_rows),
        "eligible": sum(r["eligible"] for r in screening_rows), "passed": sum(r["passed"] for r in screening_rows),
        "official_samples": official, "samples": counts, "anonymized": sum(s.anonymized_pair for s in samples),
        "drift": sum(s.drift_repeat for s in samples),
    }
    _stage(store, "plan", summary, files)
    return summary


def _screening_row(verdict: Screen) -> dict:
    return {
        "code": verdict.code, "s0": verdict.s0.isoformat(), "eligible": verdict.eligible, "passed": verdict.passed,
        "close_as_traded": None if verdict.close_as_traded is None else str(verdict.close_as_traded),
        "routes": verdict.routes, "turnover_avg_20d": verdict.turnover_avg_20d, "reason": verdict.reason,
    }


# ----------------------------------------------------------------- Phase B: the cohort


def load_cohort(root: Path, cohort_id: str, *, allow_stopped: bool = False) -> dict:
    """The cohort, refused when its frozen protocol no longer holds or it has been stopped."""

    store = phase_b.cohort_store(root, cohort_id)
    if not store.exists("cohort.json"):
        raise EvaluationError(f"no Phase B cohort {cohort_id!r}; create it with phase-b-init")
    cohort = store.read_json("cohort.json")
    if phase_b.fingerprint(cohort["frozen"]) != cohort["frozen_fingerprint"]:
        raise EvaluationError(f"cohort {cohort_id}: cohort.json does not match its own fingerprint")
    try:
        current = phase_b.frozen_protocol(load_method(), evaluation_version=EVALUATION_VERSION)
    except MethodError as exc:
        raise EvaluationError(f"cohort {cohort_id}: the method cannot be verified: {exc}") from exc
    differences = phase_b.frozen_differences(cohort["frozen"], current)
    if differences:
        raise EvaluationError(f"Phase B is frozen, and these differ from cohort {cohort_id}'s protocol: "
                              f"{differences}. A changed protocol is a new cohort")
    stops = phase_b.cohort_stops(root, cohort_id)
    if stops and not allow_stopped:
        raise EvaluationError(f"cohort {cohort_id} is stopped ({stops[-1]['reason']}, S0 {stops[-1]['s0']}); "
                              "nothing more runs in it")
    return cohort


def phase_b_init(root: Path, cohort_id: str, *, experiment_seed: str, clock: Clock | None = None) -> dict:
    """Create a cohort: its protocol frozen, its limits and its pre-registered subgroups written down. Sends nothing."""

    store = phase_b.cohort_store(root, cohort_id)
    if store.exists("cohort.json"):
        raise StoreError(f"cohort {cohort_id} exists; a cohort is created once")
    if not str(experiment_seed).strip():
        raise EvaluationError("a cohort needs a fixed experiment seed")
    frozen = phase_b.frozen_protocol(load_method(), evaluation_version=EVALUATION_VERSION)
    cohort = {
        "cohort_id": cohort_id,
        "phase": "B",
        "evaluation_version": EVALUATION_VERSION,
        "created_at": _now(clock).isoformat(),
        "experiment_seed": str(experiment_seed),
        "prospective_start": PHASE_B_START.isoformat(),
        "per_day": phase_b.per_day(),
        "target_business_days": phase_b.TARGET_BUSINESS_DAYS,
        "targets": {"primary": PRIMARY_PER_DAY * phase_b.TARGET_BUSINESS_DAYS,
                    "control": CONTROL_PER_DAY * phase_b.TARGET_BUSINESS_DAYS,
                    "main": (PRIMARY_PER_DAY + CONTROL_PER_DAY) * phase_b.TARGET_BUSINESS_DAYS},
        "provider": TYPESAFE_DIRECT,
        "requested_model": phase_b.PINNED_MODEL,
        "pinned_served_model": phase_b.PINNED_MODEL,
        "model_pinning": _model_metadata(TYPESAFE_DIRECT, pinned=phase_b.PINNED_MODEL),
        "gateway": "not used for the cohort: its answers carry no version, so it cannot be held to the pin",
        "pacing": "one request every 5 seconds (surge.evaluation.pacing, typesafe-direct)",
        "budget": {"global_hard_cap_usd": str(phase_b.GLOBAL_HARD_CAP_USD),
                   "daily_budget_usd": str(phase_b.DAILY_BUDGET_USD),
                   "daily_max_requests": MAX_REQUESTS_PER_DAY,
                   "credit": "the latest TypeSafe console snapshot less the spend recorded since, until the "
                             "expiry it showed; no paid credit"},
        "send_window": "from the S0 session end plus twice Yahoo's delay (every bar final, D-262) until 09:00 JST "
                       "on the next weekday (the earliest S1 can open)",
        "frozen": frozen,
        "frozen_fingerprint": phase_b.fingerprint(frozen),
        "preregistered": phase_b.PREREGISTERED,
        "not_introduced": phase_b.NOT_INTRODUCED,
        "storage": {"database_writes": "none", "teacher_admissible": False},
    }
    store.write_json("cohort.json", cohort)
    return {"cohort_id": cohort_id, "frozen_fingerprint": cohort["frozen_fingerprint"],
            "prospective_start": cohort["prospective_start"], "per_day": cohort["per_day"],
            "requested_model": cohort["requested_model"], "budget": cohort["budget"]}


def record_credit(root: Path, *, balance_usd: Decimal, confirmed_at: datetime, expires_on: date,
                  displayed_expiry: str | None = None, clock: Clock | None = None,
                  source: str = "console.typesafe.ai/settings/billing, read by the operator") -> dict:
    """One reading of TypeSafe's console. The expiry is taken as the start (UTC) of the date it shows."""

    return record_credit_snapshot(
        root, balance_usd=balance_usd, confirmed_at=confirmed_at,
        expires_at=datetime.combine(expires_on, clock_time(0), tzinfo=UTC),
        displayed_expiry=displayed_expiry or expires_on.isoformat(), source=source, now=_now(clock))


def _archive_writer():
    buffer = io.BytesIO()
    return buffer, gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0)


def plan_day(root: Path, cohort_id: str, s0: date, *, clock: Clock | None = None, yahoo=None, fetch_issues=None,
             sleep=time.sleep) -> dict:
    """One business day: every domestic common stock screened at S0, every result kept, then the day's sample.

    Refused before S0's bars are final, once S1 may have opened, before
    2026-09-24, on a day the data shows no session, and when less than 95% of
    the universe's histories could be read. Nothing is written until all of it
    has been computed.
    """

    from surge.evaluation.universe import fetch_listed_issues
    from surge.providers.yahoo_finance import YahooSession

    now = _now(clock)
    cohort = load_cohort(root, cohort_id)
    try:
        check_s0(s0)
    except SelectionError as exc:
        raise EvaluationError(str(exc)) from exc
    if now < phase_b.close_confirmed_at(s0):
        raise EvaluationError(f"S0 {s0}: its bars are final from {phase_b.close_confirmed_at(s0).isoformat()}, "
                              "not before")
    if now >= phase_b.send_deadline(s0):
        raise SendWindowClosed(f"S0 {s0}: S1 may have opened ({phase_b.send_deadline(s0).isoformat()}); the day "
                               "can no longer be predicted prospectively")
    store = phase_b.day_store(root, cohort_id, s0)
    if store.exists("manifest.json"):
        raise StoreError(f"{store.run_id} has already been planned")
    done = phase_b.completed_days(root, cohort_id)
    if len(done) >= cohort["target_business_days"]:
        raise EvaluationError(f"cohort {cohort_id} has its {cohort['target_business_days']} business days "
                              f"({done[0]} to {done[-1]}); no further day is planned")
    method = load_method()

    issues, workbook_sha256 = (fetch_issues or fetch_listed_issues)()
    issues = sorted(issues, key=lambda i: i.code)
    start = s0 - timedelta(days=phase_b.HISTORY_LOOKBACK_DAYS)
    yahoo = yahoo or YahooSession()
    buffer, archive = _archive_writer()
    bar_counts: Counter = Counter()
    screens, screening_rows, failures, read = [], [], [], 0
    for count, issue in enumerate(issues, start=1):
        if count % 200 == 0:
            _progress(f"{s0}: {count}/{len(issues)} read ({len(failures)} failed)")
        chart, history, failure = _fetch_one(yahoo, issue.code, start, now, sleep)
        sleep(YAHOO_PAUSE_SECONDS)
        if failure is not None:
            failures.append(failure)
            continue
        read += 1
        archive.write(_chart_line(chart))
        bar_counts.update({bar.trade_date for bar in history.bars})
        verdict = screen(history, s0)
        screens.append(replace(verdict, series=None, features=None))
        screening_rows.append(_screening_row(verdict))
    archive.close()
    coverage = read / len(issues) if issues else 0.0
    if coverage < phase_b.MIN_UNIVERSE_COVERAGE:
        raise EvaluationError(f"S0 {s0}: {read} of {len(issues)} histories read ({coverage:.1%}), below "
                              f"{phase_b.MIN_UNIVERSE_COVERAGE:.0%}; the day is not drawn from")
    sessions = sessions_from_counts(bar_counts, read)
    if s0 not in sessions:
        raise EvaluationError(f"{s0} is not a session in the data: fewer than 30% of the securities have a bar")

    by_code = {i.code: i for i in issues}
    selection = select_day(screens, by_code, s0=s0, evaluation_version=cohort["evaluation_version"],
                           experiment_seed=cohort["experiment_seed"])
    if not selection.samples:
        raise EvaluationError(f"S0 {s0}: no eligible security passed the screener; the day has nothing to ask")

    files: dict[str, str] = {}
    files[PRICE_ARCHIVE] = store.write_bytes(PRICE_ARCHIVE, buffer.getvalue())
    files["inputs/universe.json"] = store.write_json("inputs/universe.json", {
        "workbook_sha256": workbook_sha256, "domestic_common_stock": len(issues), "histories_read": read,
        "coverage": coverage, "failures": failures, "issues": [vars(i) for i in issues],
    })
    files["sessions.json"] = store.write_json("sessions.json", {
        "rule": "a date on which at least 30% of the securities read have a bar (D-142: no guessed calendar)",
        "sessions": [d.isoformat() for d in sessions],
    })
    files["screening.parquet"] = store.write_parquet("screening.parquet", screening_rows)
    files["candidates.jsonl"] = store.write_jsonl("candidates.jsonl", selection.candidates)
    files["candidates.parquet"] = store.write_parquet("candidates.parquet", selection.candidates)
    rows = [s.row() for s in selection.samples]
    files["population.jsonl"] = store.write_jsonl("population.jsonl", rows)
    files["population.parquet"] = store.write_parquet("population.parquet", rows)
    manifest = {
        "run_id": store.run_id,
        "evaluation_version": EVALUATION_VERSION,
        "phase": "B",
        "cohort_id": cohort["cohort_id"],
        "cohort_fingerprint": cohort["frozen_fingerprint"],
        "s0": s0.isoformat(),
        "started_at": now.isoformat(),
        "model": cohort["requested_model"],
        "provider": TYPESAFE_DIRECT,
        "fallback_provider": None,
        "model_version_metadata": _model_metadata(TYPESAFE_DIRECT, pinned=cohort["requested_model"]),
        "question_schema_hash": question_schema_hash(),
        "method": method.manifest_entry(),
        "input_building_code": code_version(),
        "population_definition": {
            "market": "JP",
            "universe": "JPX listed issues workbook as published on S0's evening, domestic common stock in "
                        "PRIME / STANDARD / GROWTH, every security",
            "workbook_sha256": workbook_sha256,
            "universe_read": {"issues": len(issues), "histories_read": read, "failed": len(failures),
                              "coverage": coverage, "minimum_coverage": phase_b.MIN_UNIVERSE_COVERAGE},
            "history_start": start.isoformat(),
            "s0": s0.isoformat(),
            "screener": SCREENER_DEFINITION,
            "eligible": "a bar on S0 and the S0 close as traded <= 3,000 yen",
            "kept": "every screening result (screening.parquet) and every eligible candidate with its route "
                    "membership, key, rank, selection and probability (candidates.*), before the sample is drawn",
            "selection": {"version": SELECTION_VERSION, "evaluation_version": cohort["evaluation_version"],
                          "experiment_seed": cohort["experiment_seed"], "per_day": phase_b.per_day()},
            "route_d": "unchanged; D-only candidates drawn like any other; subgroups pre-registered in cohort.json",
            "cohorts_pooled": False,
            "limitations": PHASE_B_LIMITATIONS,
        },
        "outcome_definition": OUTCOME_DEFINITION,
        "storage": {"database_writes": "none", "teacher_admissible": False},
    }
    files["manifest.json"] = store.write_json("manifest.json", manifest)
    summary = {"s0": s0.isoformat(), "issues": len(issues), "histories_read": read, "failed": len(failures),
               "coverage": coverage, **selection.summary}
    _stage(store, "plan", summary, files)
    return summary


def _stored_selection(store: RunStore, cohort: dict, s0: date):
    """The day's sample drawn again from what plan-day kept: the eligible candidates and the universe."""

    issues = {i["code"]: ListedIssue(**i) for i in store.read_json("inputs/universe.json")["issues"]}
    screens = [Screen(code=r["code"], s0=s0, eligible=True, passed=r["passed"],
                      close_as_traded=Decimal(r["close_as_traded"]), routes=r["routes"],
                      turnover_avg_20d=r["turnover_avg_20d"])
               for r in store.read_jsonl("candidates.jsonl")]
    return select_day(screens, issues, s0=s0, evaluation_version=cohort["evaluation_version"],
                      experiment_seed=cohort["experiment_seed"])


def _day_cohort(store: RunStore, manifest: dict, *, allow_stopped: bool = False) -> dict:
    cohort = load_cohort(store.root, manifest["cohort_id"], allow_stopped=allow_stopped)
    if manifest["cohort_fingerprint"] != cohort["frozen_fingerprint"]:
        raise EvaluationError(f"{store.run_id} was planned under another protocol than cohort "
                              f"{cohort['cohort_id']}'s")
    return cohort


# ----------------------------------------------------------------- build


def build(store: RunStore, *, yanoshin=None) -> dict:
    """The state and the exact bytes of every request. No model request."""

    from surge.news.sources.yanoshin_tdnet import YanoshinTdnetSource

    _verify_stage(store, "plan")
    if store.exists("stage-build.json"):
        raise StoreError(f"run {store.run_id} has already been built")
    manifest = store.read_json("manifest.json")
    _code_matches(manifest)
    method = _method_matches(manifest)
    if manifest["phase"] == "B":
        _day_cohort(store, manifest)
    samples = _load_samples(store)
    histories = _load_histories(store, {s.code for s in samples})
    yanoshin = yanoshin or YanoshinTdnetSource()

    files: dict[str, str] = {}
    disclosures = {}
    for code in sorted({s.code for s in samples}):
        result = yanoshin.fetch_for_codes([code])
        oldest = min((it.pubdate for it in result.items), default=None)
        record = {
            "code": code, "fetched_at": result.fetched_at.isoformat(), "endpoint": result.endpoint,
            "response_sha256": result.response_sha256, "total_count": result.total_count,
            "returned": len(result.items), "limit": yanoshin.limit,
            "oldest_returned": None if oldest is None else oldest.isoformat(),
            # Only this security's rows: the index is keyed by issuer code, and
            # another issuer's titles must never reach this state.
            "items": [{"id": it.yanoshin_id, "pubdate": it.pubdate.isoformat(), "title": it.title}
                      for it in result.items if it.code.normalised == code],
        }
        files[f"inputs/tdnet/{code}.json"] = store.write_json(f"inputs/tdnet/{code}.json", record)
        disclosures[code] = record

    requests = []
    for sample in samples:
        screened = screen(histories[sample.code], sample.s0)
        if (screened.passed, screened.close_as_traded) != (sample.cohort == "PRIMARY", sample.s0_close_as_traded):
            raise EvaluationError(f"{sample.sample_id}: the screening replay did not reproduce the plan")
        record = disclosures[sample.code]
        items = [SimpleNamespace(pubdate=datetime.fromisoformat(i["pubdate"]), title=i["title"])
                 for i in record["items"]]
        chosen = select_disclosures(items, sample.s0)
        coverage = disclosure_coverage(
            sample.s0, returned=record["returned"], limit=record["limit"],
            oldest_returned=None if record["oldest_returned"] is None else datetime.fromisoformat(
                record["oldest_returned"]),
        )
        main = None
        for variant, anonymized in (("main", False), ("anonymized", True)):
            if anonymized and not sample.anonymized_pair:
                continue
            state = build_state(sample, screened, chosen, canonical=method.canonical.text,
                                addenda=[a.text for a in method.addenda], anonymized=anonymized)
            raw = json.dumps(request_body(state), ensure_ascii=False).encode("utf-8")
            name = f"requests/{sample.sample_id}.{variant}.json"
            sha = store.write_bytes(name, raw)
            files[name] = sha
            tokens = _o200k(raw.decode("utf-8"))
            row = {
                "sample_id": sample.sample_id, "cohort": sample.cohort, "variant": variant, "file": name,
                "sha256": sha, "bytes": len(raw), "o200k_tokens": tokens,
                "estimated_jev_tokens": estimated_jev_tokens(tokens), "estimated_usd": str(estimate_usd(tokens)),
                "disclosure_titles": len(chosen), "disclosure_coverage": coverage,
            }
            requests.append(row)
            if variant == "main":
                main = row
        if sample.drift_repeat:
            requests.append({**main, "variant": "drift"})

    files["requests.jsonl"] = store.write_jsonl("requests.jsonl", requests)
    tokens = [r["o200k_tokens"] for r in requests]
    summary = {
        "requests": len(requests),
        "by_variant": {v: sum(r["variant"] == v for r in requests) for v in ("main", "anonymized", "drift")},
        "o200k_tokens": {"min": min(tokens), "max": max(tokens), "mean": round(sum(tokens) / len(tokens))},
        "estimated_usd_total": str(sum((Decimal(r["estimated_usd"]) for r in requests), Decimal(0))),
        "disclosure_coverage": {c: sum(r["disclosure_coverage"] == c for r in requests if r["variant"] == "main")
                                for c in ("COMPLETE", "INCOMPLETE")},
    }
    _stage(store, "build", summary, files)
    return summary


# ----------------------------------------------------------------- outcomes


def freeze_outcomes(store: RunStore, *, clock: Clock | None = None, yahoo=None, sleep=time.sleep) -> dict:
    """Phase A: every outcome fixed from prices before a single answer exists.

    Phase B: after T+20 - the answers were given before S1 could open - from
    prices read again then, by the cohort's frozen outcome code; the answers
    are not read.
    """

    _verify_stage(store, "plan")
    if store.exists("stage-outcomes.json"):
        raise StoreError(f"outcomes for {store.run_id} are already frozen")
    manifest = store.read_json("manifest.json")
    if manifest["phase"] == "B":
        return _freeze_outcomes_b(store, manifest, clock=clock, yahoo=yahoo, sleep=sleep)
    _code_matches(manifest)
    if (store.path / "responses").exists() and any((store.path / "responses").iterdir()):
        raise EvaluationError("answers exist already; outcomes must be frozen before any request is sent")
    samples, histories, sessions = _load_samples(store), _load_histories(store), _sessions(store)
    rows = [compute_outcome(s, histories[s.code], sessions).row() for s in samples]
    files = {
        "outcomes.jsonl": store.write_jsonl("outcomes.jsonl", rows),
        "outcomes.parquet": store.write_parquet("outcomes.parquet", rows),
    }
    summary = {"frozen_at": datetime.now(UTC).isoformat(),
               "resolution": {r: sum(o["resolution"] == r for o in rows)
                              for r in ("RESOLVED", "UNRESOLVED_MISSING_DATA", "PENDING")}}
    _stage(store, "outcomes", summary, files)
    return summary


def _freeze_outcomes_b(store: RunStore, manifest: dict, *, clock: Clock | None, yahoo, sleep) -> dict:
    from surge.providers.yahoo_finance import YahooSession

    _verify_stage(store, "run")
    # A cohort stopped later (a version change) leaves the days answered before it valid.
    _day_cohort(store, manifest, allow_stopped=True)
    now = _now(clock)
    s0 = date.fromisoformat(manifest["s0"])
    samples = _load_samples(store)
    codes = sorted({s.code for s in samples})
    yahoo = yahoo or YahooSession()
    buffer, archive = _archive_writer()
    histories, failures, bar_counts = {}, [], Counter()
    for code in codes:
        chart, history, failure = _fetch_one(yahoo, code, s0 - timedelta(days=OUTCOME_LOOKBACK_DAYS), now, sleep)
        sleep(YAHOO_PAUSE_SECONDS)
        if failure is not None:
            failures.append(failure)
            continue
        archive.write(_chart_line(chart))
        histories[code] = history
        bar_counts.update({bar.trade_date for bar in history.bars})
    archive.close()
    if failures:
        raise EvaluationError(f"{len(failures)} of {len(codes)} histories could not be read; outcomes are not "
                              f"frozen: {failures[:3]}")
    sessions = sessions_from_counts(bar_counts, len(histories))
    after = [d for d in sessions if d > s0]
    if s0 not in sessions or len(after) < HORIZON:
        raise EvaluationError(f"S0 {s0}: {len(after)} sessions after it so far; T+{HORIZON} has not closed")
    t_last = after[HORIZON - 1]
    if now < phase_b.close_confirmed_at(t_last):
        raise EvaluationError(f"T+{HORIZON} ({t_last}) is final from {phase_b.close_confirmed_at(t_last).isoformat()}")
    rows, problems = [], {}
    for sample in samples:
        try:
            rows.append(compute_outcome(sample, histories[sample.code], sessions).row())
        except ValueError as exc:
            problems[sample.sample_id] = str(exc)[:200]
    if problems:
        raise EvaluationError(f"outcomes are not frozen: {problems}")
    files = {
        OUTCOME_PRICE_ARCHIVE: store.write_bytes(OUTCOME_PRICE_ARCHIVE, buffer.getvalue()),
        "outcome-inputs/sessions.json": store.write_json("outcome-inputs/sessions.json", {
            "rule": "a date on which at least 30% of the day's sample securities have a bar (D-142)",
            "fetched_at": now.isoformat(), "sessions": [d.isoformat() for d in sessions]}),
        "outcomes.jsonl": store.write_jsonl("outcomes.jsonl", rows),
        "outcomes.parquet": store.write_parquet("outcomes.parquet", rows),
    }
    summary = {"frozen_at": now.isoformat(), "t_plus_20": t_last.isoformat(), "answers_read": False,
               "prices_read_after_t_plus_20": True,
               "resolution": {r: sum(o["resolution"] == r for o in rows)
                              for r in ("RESOLVED", "UNRESOLVED_MISSING_DATA", "PENDING")}}
    _stage(store, "outcomes", summary, files)
    return summary


# ----------------------------------------------------------------- preflight


def _next_name(store: RunStore, stem: str) -> str:
    n = 1
    while store.exists(f"{stem}-{n}.json"):
        n += 1
    return f"{stem}-{n}.json"


def _request_problems(store: RunStore, sample: Sample, by_key: dict, expected_questions: dict) -> tuple[list, dict]:
    problems, bodies = [], {}
    for variant in ("main", "anonymized"):
        row = by_key.get((sample.sample_id, variant))
        if row is None:
            if variant == "main" or sample.anonymized_pair:
                problems.append(f"no {variant} request")
            continue
        store.verify(row["file"], row["sha256"])
        body = json.loads((store.path / row["file"]).read_bytes())
        if body["model"] != GATEWAY_MODEL or body["questions"] != expected_questions:
            problems.append(f"the {variant} request's model or questions differ from the manifest's")
        if row["estimated_jev_tokens"] > CONTEXT_TOKENS:
            problems.append(f"the {variant} request may exceed Jev's {CONTEXT_TOKENS}-token context")
        bodies[variant] = body
    if sample.drift_repeat and by_key.get((sample.sample_id, "drift"), {}).get("sha256") != \
            by_key.get((sample.sample_id, "main"), {}).get("sha256"):
        problems.append("the drift request is not byte-identical to the main request")
    return problems, bodies


def preflight(store: RunStore, *, budget: Budget, runner: Path | None = RUNNER, provider: str | None = None,
              clock: Clock | None = None) -> dict:
    """Every check that can be made without sending: leakage, schema, budget, credit.

    The credit is the provider's: the Gateway's balance through the runner, or
    TypeSafe's latest console snapshot less the spend recorded since (TypeSafe
    has no balance API). A Phase B day has no outcome yet; its own checks are
    ``_preflight_b``'s.
    """

    manifest = store.read_json("manifest.json") if store.exists("manifest.json") else None
    if manifest is not None and manifest["phase"] == "B":
        return _preflight_b(store, manifest, budget=budget, provider=provider, clock=clock)
    for stage in ("plan", "build", "outcomes"):
        _verify_stage(store, stage)
    manifest = store.read_json("manifest.json")
    provider = _provider_of(manifest, provider)
    _code_matches(manifest)
    _method_matches(manifest)
    canonical_sha = manifest["method"]["canonical"]["sha256"]
    addenda_sha = [a["sha256"] for a in manifest["method"]["addenda_newer_overrides_older"]]
    samples, histories, sessions = _load_samples(store), _load_histories(store), _sessions(store)
    requests = store.read_jsonl("requests.jsonl")
    frozen = {o["sample_id"]: o for o in store.read_jsonl("outcomes.jsonl")}
    by_key = {(r["sample_id"], r["variant"]): r for r in requests}
    expected_questions = to_gateway(questions())

    violations: dict[str, list[str]] = {}
    for sample in samples:
        problems, bodies = _request_problems(store, sample, by_key, expected_questions)
        outcome = compute_outcome(sample, histories[sample.code], sessions)
        if outcome.row() != frozen.get(sample.sample_id):
            problems.append("the frozen outcome does not recompute to the same values")
        if outcome.resolution != "RESOLVED":
            problems.append(f"outcome is {outcome.resolution}")
        if "main" in bodies:
            problems += check_sample(
                sample, screen(histories[sample.code], sample.s0), bodies["main"]["state"], outcome,
                canonical_sha256=canonical_sha, addenda_sha256=addenda_sha,
                anonymized_state=bodies["anonymized"]["state"] if "anonymized" in bodies else None,
            )
        if problems:
            violations[sample.sample_id] = problems

    estimates = [Decimal(r["estimated_usd"]) for r in requests]
    credits = _credits(provider, store, runner=runner, now=_now(clock))
    budget_problems = plan_problems(budget, estimates, credits)
    tokens = [r["o200k_tokens"] for r in requests]
    result = {
        "checked_at": _now(clock).isoformat(),
        "run_id": store.run_id,
        "provider": provider,
        "samples": {c: sum(s.cohort == c for s in samples) for c in ("PRIMARY", "CONTROL")},
        "requests": {v: sum(r["variant"] == v for r in requests) for v in ("main", "anonymized", "drift")},
        "requests_total": len(requests),
        "leakage_and_integrity": {"samples_checked": len(samples), "samples_with_violations": len(violations),
                                  "violations": violations},
        "o200k_tokens": {"min": min(tokens), "max": max(tokens), "mean": round(sum(tokens) / len(tokens))},
        "estimated_jev_tokens_max": max(r["estimated_jev_tokens"] for r in requests),
        "estimated_usd_total": str(sum(estimates, Decimal(0))),
        "budget": {"max_usd": str(budget.max_usd), "max_requests": budget.max_requests},
        "credits": credits,
        "budget_problems": budget_problems,
        "ready_to_send": not violations and not budget_problems,
    }
    store.write_json(_next_name(store, "preflight"), result)
    return result


def _preflight_b(store: RunStore, manifest: dict, *, budget: Budget, provider: str | None,
                 clock: Clock | None) -> dict:
    """A Phase B day before it is sent: the cohort's protocol, the day's limits, leakage, window, money."""

    for stage in ("plan", "build"):
        _verify_stage(store, stage)
    now = _now(clock)
    cohort = _day_cohort(store, manifest)
    if _provider_of(manifest, provider) != TYPESAFE_DIRECT:
        raise EvaluationError(f"a Phase B day is sent to TypeSafe direct pinned to {cohort['requested_model']}; "
                              "the Gateway's answers carry no version and cannot join the cohort")
    _code_matches(manifest)
    _method_matches(manifest)
    s0 = date.fromisoformat(manifest["s0"])
    try:
        check_s0(s0)
    except SelectionError as exc:
        raise EvaluationError(str(exc)) from exc
    canonical_sha = manifest["method"]["canonical"]["sha256"]
    addenda_sha = [a["sha256"] for a in manifest["method"]["addenda_newer_overrides_older"]]
    samples = _load_samples(store)
    histories = _load_histories(store, {s.code for s in samples})
    requests = store.read_jsonl("requests.jsonl")
    by_key = {(r["sample_id"], r["variant"]): r for r in requests}
    expected_questions = to_gateway(questions())

    redrawn = _stored_selection(store, cohort, s0)
    selection_reproduced = selection_signature(redrawn.samples) == selection_signature(samples)
    violations: dict[str, list[str]] = {}
    for sample in samples:
        problems, bodies = _request_problems(store, sample, by_key, expected_questions)
        if sample.s0 != s0:
            problems.append(f"the sample's S0 {sample.s0} is not the day's")
        if "main" in bodies:
            problems += check_prospective_sample(
                sample, screen(histories[sample.code], s0), bodies["main"]["state"],
                canonical_sha256=canonical_sha, addenda_sha256=addenda_sha,
                anonymized_state=bodies["anonymized"]["state"] if "anonymized" in bodies else None,
            )
        if problems:
            violations[sample.sample_id] = problems

    counts = {v: sum(r["variant"] == v for r in requests) for v in VARIANTS}
    cohorts = {c: sum(s.cohort == c for s in samples) for c in ("PRIMARY", "CONTROL")}
    limit_problems = [
        message for over, message in (
            (cohorts["PRIMARY"] > PRIMARY_PER_DAY, f"{cohorts['PRIMARY']} Primary, the day allows {PRIMARY_PER_DAY}"),
            (cohorts["CONTROL"] > CONTROL_PER_DAY, f"{cohorts['CONTROL']} Control, the day allows {CONTROL_PER_DAY}"),
            (counts["anonymized"] > ANONYMIZED_PER_DAY,
             f"{counts['anonymized']} anonymized, the day allows {ANONYMIZED_PER_DAY}"),
            (counts["drift"] > DRIFT_PER_DAY, f"{counts['drift']} drift, the day allows {DRIFT_PER_DAY}"),
            (len(requests) > MAX_REQUESTS_PER_DAY, f"{len(requests)} requests, the day allows {MAX_REQUESTS_PER_DAY}"),
            (budget.max_requests > MAX_REQUESTS_PER_DAY,
             f"a budget of {budget.max_requests} requests, the day allows {MAX_REQUESTS_PER_DAY}"),
        ) if over
    ]
    if not selection_reproduced:
        limit_problems.append("the day's sample does not come out the same when drawn again from its candidates")
    if store.exists("run-stopped-1.json"):
        limit_problems.append("the day stopped earlier (run-stopped-*.json); a stop is looked at, not resumed")

    estimates = [Decimal(r["estimated_usd"]) for r in requests]
    credits = direct_credit_state(store.root, now=now)
    budget_problems = plan_problems(budget, estimates, credits)
    cap = Decimal(cohort["budget"]["global_hard_cap_usd"])
    spent_before = phase_b.cohort_spend(store.root, cohort["cohort_id"], exclude=s0)
    day_spent = phase_b.day_spend(store)
    day_estimate = sum(estimates, Decimal(0))
    if spent_before + day_spent + day_estimate > cap:
        budget_problems.append(f"the cohort has spent ${spent_before + day_spent}; with this day's ${day_estimate} "
                               f"estimated it would pass Phase B's ${cap} cap")
    window = {"opens_at": phase_b.close_confirmed_at(s0).isoformat(),
              "closes_at": phase_b.send_deadline(s0).isoformat(), "checked_at": now.isoformat()}
    window_problems = []
    if now < phase_b.close_confirmed_at(s0):
        window_problems.append("the day's bars are not final yet")
    if now >= phase_b.send_deadline(s0):
        window_problems.append("the send window has closed: S1 may have opened")
    tokens = [r["o200k_tokens"] for r in requests]
    result = {
        "checked_at": now.isoformat(),
        "run_id": store.run_id,
        "phase": "B",
        "cohort_id": cohort["cohort_id"],
        "s0": s0.isoformat(),
        "provider": TYPESAFE_DIRECT,
        "requested_model": cohort["requested_model"],
        "pinned_served_model": cohort["pinned_served_model"],
        "samples": cohorts,
        "requests": counts,
        "requests_total": len(requests),
        "selection_reproduced": selection_reproduced,
        "limit_problems": limit_problems,
        "leakage_and_integrity": {"samples_checked": len(samples), "samples_with_violations": len(violations),
                                  "violations": violations},
        "o200k_tokens": {"min": min(tokens), "max": max(tokens), "mean": round(sum(tokens) / len(tokens))},
        "estimated_jev_tokens_max": max(r["estimated_jev_tokens"] for r in requests),
        "estimated_usd_total": str(day_estimate),
        "budget": {"max_usd": str(budget.max_usd), "max_requests": budget.max_requests},
        "global_cap": {"cap_usd": str(cap), "spent_before_usd": str(spent_before), "day_spent_usd": str(day_spent),
                       "after_this_day_estimated_usd": str(spent_before + day_spent + day_estimate)},
        "credits": credits,
        "budget_problems": budget_problems,
        "send_window": window,
        "window_problems": window_problems,
        "ready_to_send": not (violations or limit_problems or budget_problems or window_problems),
    }
    store.write_json(_next_name(store, "preflight"), result)
    return result


# ----------------------------------------------------------------- run and report


def _latest(store: RunStore, stem: str) -> dict | None:
    n, latest = 1, None
    while store.exists(f"{stem}-{n}.json"):
        latest = store.read_json(f"{stem}-{n}.json")
        n += 1
    return latest


def _response_name(row: dict, raw: bool = False) -> str:
    return f"responses/{row['sample_id']}.{row['variant']}{'.raw' if raw else ''}.json"


def response_problems(record: dict, row: dict, *, provider: str = VERCEL_GATEWAY,
                      pinned_model: str | None = None) -> list[str]:
    """Why the run must stop after this answer (D-270, D-275): any one of these ends it.

    An error; an incomplete answer; a model or provider other than Jev's - for
    TypeSafe direct, a served version other than the one the run is pinned to;
    an answer recorded against another request; a missing cost, or one above
    the request's (conservative) estimate; input tokens past the estimate.
    """

    if record.get("status") != "ok":
        error = record.get("error")
        return [f"{provider} error: {error if isinstance(error, dict) else str(error)[:160]}"]
    problems = []
    if record.get("completeness_problems"):
        problems.append(f"schema incomplete: {record['completeness_problems']}")
    model = record.get("model")
    if provider == TYPESAFE_DIRECT:
        if not isinstance(model, str) or not SERVED_VERSION.match(model):
            problems.append(f"unexpected model {model!r}")
        elif pinned_model is not None and model != pinned_model:
            problems.append(f"the served model changed from {pinned_model} to {model} within the run")
    elif model != GATEWAY_MODEL:
        problems.append(f"unexpected model {model!r}")
    if record.get("resolved_provider") != EXPECTED_PROVIDER:
        problems.append(f"unexpected provider {record.get('resolved_provider')!r}")
    if record.get("request_sha256") != row["sha256"]:
        problems.append("the answer is recorded against another request")
    cost = record_cost(record)
    if cost is None:
        problems.append("no cost for this answer")
    elif cost > Decimal(row["estimated_usd"]):
        problems.append(f"cost ${cost} is above the request's estimate ${row['estimated_usd']}")
    tokens = (record.get("usage") or {}).get("inputTokens")
    if not isinstance(tokens, int) or not 0 < tokens <= row["estimated_jev_tokens"]:
        problems.append(f"input tokens {tokens!r} outside the estimate {row['estimated_jev_tokens']}")
    return problems


#: The Gateway runner's HTTP facts (D-273); kept under this name for callers of Phase A.
http_facts = gateway_http_facts


def _prediction_rows(store: RunStore) -> list[dict]:
    manifest = store.read_json("manifest.json")
    samples = {r["sample_id"]: r for r in store.read_jsonl("population.jsonl")}
    rows = []
    for request in store.read_jsonl("requests.jsonl"):
        name = _response_name(request)
        if store.exists(name):
            rows.append(prediction_row(store.read_json(name), samples[request["sample_id"]],
                                       variant=request["variant"], request=request,
                                       evaluation_version=EVALUATION_VERSION,
                                       question_schema_hash=manifest["question_schema_hash"]))
    return rows


def _clock_block(clock: Clock | None, *, expires_at: datetime | None, deadline: datetime | None,
                 s0: date | None) -> str | None:
    """Why nothing may be sent at this moment - checked after the pacing wait, right before a send."""

    now = _now(clock)
    if expires_at is not None and now >= expires_at:
        return (f"the TypeSafe credit expired at {expires_at.isoformat()}: confirm the new console balance once "
                "(record-credit) before anything more is sent")
    if deadline is not None and now >= deadline:
        return f"S1 may have opened ({deadline.isoformat()}): nothing more of {s0} is sent"
    return None


def _rate_limit_wait(record: dict, attempt: int) -> float:
    stated = (record.get("http") or {}).get("retry_after_seconds")
    return float(stated) if stated is not None else FALLBACK_BACKOFF_SECONDS * 2 ** attempt


def run(store: RunStore, *, budget: Budget, runner: Path = RUNNER, pacer: Pacer | None = None,
        provider: str | None = None, transport=None, clock: Clock | None = None) -> dict:
    """Send the planned requests - main, then anonymized, then drift - inside the hard budget.

    Through the run's provider (the manifest's, or the Gateway fallback when
    asked for, except in Phase B), paced by its policy: TypeSafe direct one
    request every 5 seconds, the Gateway one every 5 minutes (D-274, D-275).
    Every answer records when it was requested, the previous successful
    request, the rolling counts and the HTTP facts of a failure; the raw
    response is kept beside it. A 429 from TypeSafe direct waits what the
    server asks (``retry_after_ms`` / Retry-After), or a doubling backoff when
    it asks nothing, and tries the same request again - at most 3 times, never
    longer than 30 minutes at once; a Gateway 429 stops the run.

    Before every request: the run's budget, the credit's expiry and, for a
    Phase B day, the send window (S1's open) and the cohort's $2.50 cap. After
    every answer of a Phase B day, a served version other than the pinned one
    stops the day and the whole cohort.
    """

    _verify_stage(store, "plan")
    manifest = store.read_json("manifest.json")
    is_b = manifest["phase"] == "B"
    for stage in ("plan", "build") if is_b else ("plan", "build", "outcomes"):
        _verify_stage(store, stage)
    if store.exists("stage-run.json"):
        raise EvaluationError(f"run {store.run_id} has already sent every request")
    if store.exists("run-stopped-1.json"):
        # A stop is looked at, not resumed past.
        raise EvaluationError(f"run {store.run_id} stopped earlier (run-stopped-*.json); it is not resumed")
    checked = _latest(store, "preflight")
    if not checked or not checked["ready_to_send"]:
        raise EvaluationError("the latest preflight is missing or not ready to send")
    if checked["budget"] != {"max_usd": str(budget.max_usd), "max_requests": budget.max_requests}:
        raise EvaluationError(f"the budget differs from the one preflight checked ({checked['budget']})")
    provider = _provider_of(manifest, provider)
    if checked.get("provider", VERCEL_GATEWAY) != provider:
        raise EvaluationError(f"preflight checked {checked.get('provider', VERCEL_GATEWAY)}, not {provider}")
    _code_matches(manifest)
    _method_matches(manifest)
    pinned_model = requested_model = None
    s0 = deadline = cap = cohort = None
    spent_other_days = Decimal(0)
    if is_b:
        cohort = _day_cohort(store, manifest)
        if provider != TYPESAFE_DIRECT:
            raise EvaluationError("a Phase B day is sent to TypeSafe direct only; the Gateway cannot join its cohort")
        s0 = date.fromisoformat(manifest["s0"])
        deadline = phase_b.send_deadline(s0)
        cap = Decimal(cohort["budget"]["global_hard_cap_usd"])
        spent_other_days = phase_b.cohort_spend(store.root, cohort["cohort_id"], exclude=s0)
        pinned_model, requested_model = cohort["pinned_served_model"], cohort["requested_model"]
    adapter = provider_for(provider, runner=runner, transport=transport, model=requested_model)
    pacer = pacer or pacer_for(provider)

    requests = store.read_jsonl("requests.jsonl")
    order = {"main": 0, "anonymized": 1, "drift": 2}
    requests.sort(key=lambda r: order[r["variant"]])
    ledger = Ledger(budget)
    pending = []
    for row in requests:
        if store.exists(_response_name(row)):
            ledger.record(store.read_json(_response_name(row)), estimate=Decimal(row["estimated_usd"]))
        else:
            pending.append(row)
    credits = _credits(provider, store, runner=runner, now=_now(clock))
    problems = plan_problems(budget, [Decimal(r["estimated_usd"]) for r in pending], credits)
    pending_total = sum((Decimal(r["estimated_usd"]) for r in pending), Decimal(0))
    if is_b and spent_other_days + ledger.spent_usd + pending_total > cap:
        problems.append(f"${spent_other_days + ledger.spent_usd} spent and ${pending_total} to send would pass "
                        f"Phase B's ${cap} cap")
    if problems:
        raise EvaluationError(f"not sending: {problems}")
    expires_at = (datetime.fromisoformat(credits["expires_at"])
                  if provider == TYPESAFE_DIRECT and credits.get("expires_at") else None)

    stopped, last = None, None
    previous_success_at = None
    max_in_window = 0
    sends = 0  # every HTTP attempt of this invocation, in order
    for count, row in enumerate(pending, start=1):
        label = f"{row['sample_id']}.{row['variant']}"
        estimate = Decimal(row["estimated_usd"])
        try:
            ledger.check_next(estimate)
            if is_b:
                phase_b.check_cap(spent_other_days + ledger.spent_usd, estimate, cap)
            # The bytes sent are the bytes built and checked, or nothing is sent.
            store.verify(row["file"], row["sha256"])
        except (BudgetExceeded, StoreError) as exc:
            stopped = f"{label}: {exc}"
            break
        rate_limited, record, blocked = [], None, None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            raw_name = _response_name(row, raw=True)
            if attempt:
                raw_name = raw_name.replace(".raw.json", f".raw.{attempt}.json")
            raw_out = store.path / raw_name
            if raw_out.exists():
                raise StoreError(f"{raw_out} exists without its record; look at it before sending again")
            waited = pacer.wait()
            blocked = _clock_block(clock, expires_at=expires_at, deadline=deadline, s0=s0)
            if blocked:
                break
            requested_at = _now(clock).isoformat()
            in_window = pacer.mark_sent()
            in_last_60s = pacer.count_within(60.0)
            max_in_window = max(max_in_window, in_window)
            sends += 1
            record = adapter.call(store.path / row["file"], raw_out, label=label, request_sha256=row["sha256"])
            record["pacing"] = {
                "send_index": sends,
                "requested_at": requested_at,
                "runner_started_at": record.get("runner_started_at") or record.get("sent_at"),
                "previous_success_at": previous_success_at,
                "requests_in_rolling_60s": in_last_60s,
                "requests_in_policy_window": in_window,
                "waited_seconds": round(waited, 3),
                "policy": {"min_interval_seconds": pacer.min_interval, "window_seconds": pacer.window,
                           "max_in_window": pacer.max_in_window},
            }
            if record["http"]["status"] != 429 or not adapter.honours_rate_limit_wait:
                break
            wait = _rate_limit_wait(record, attempt)
            rate_limited.append({"requested_at": requested_at, "raw_file": raw_name, "http": record["http"],
                                 "wait_seconds": round(wait, 3)})
            if attempt == MAX_RATE_LIMIT_RETRIES or wait > MAX_RATE_LIMIT_WAIT_SECONDS:
                break
            if is_b and _now(clock).timestamp() + wait >= deadline.timestamp():
                break  # the wait would reach S1's open: the 429 stands and the day stops
            _progress(f"{label}: 429, waiting {wait:.1f}s as the provider asks")
            pacer.sleep(wait)
        if record is None:  # stopped by the clock before anything of this request was sent
            stopped = f"{label}: {blocked}"
            break
        record["rate_limited_attempts"] = rate_limited
        cost = record_cost(record)
        record["ledger_usd"] = str(cost if cost is not None else estimate)
        store.write_json(_response_name(row), record)
        ledger.record(record, estimate=estimate)
        last = {"label": label, "pacing": record["pacing"], "http": record["http"],
                "rate_limited_attempts": rate_limited}
        problems = response_problems(record, row, provider=provider, pinned_model=pinned_model)
        if blocked:
            problems = [blocked, *problems]
        if problems:
            stopped = f"{label}: {problems}"
            served = record.get("model")
            if is_b and record.get("status") == "ok" and served != pinned_model:
                last["cohort_stop"] = phase_b.stop_cohort(
                    store.root, cohort["cohort_id"], s0=s0, reason="served model is not the pinned one",
                    detail=f"{label}: pinned {pinned_model}, served {served!r}")
            break
        pinned_model = pinned_model or (record.get("model") if provider == TYPESAFE_DIRECT else None)
        previous_success_at = requested_at
        if count % 10 == 0:
            _progress(f"sent {count}/{len(pending)}, ${ledger.spent_usd}")

    summary = {"provider": provider, "served_model": pinned_model,
               "sent_this_time": ledger.requests - (len(requests) - len(pending)), "requests_answered":
               ledger.requests, "spent_usd": str(ledger.spent_usd), "stopped": stopped,
               "estimated_charges": ledger.estimated_charges, "credits_before": credits,
               "max_requests_in_policy_window": max_in_window}
    if is_b:
        summary["cohort_spent_usd"] = str(spent_other_days + ledger.spent_usd)
        summary["cohort_cap_usd"] = str(cap)
    if stopped is not None:
        store.write_json(_next_name(store, "run-stopped"), {**summary, "last_request": last})
    elif ledger.requests == len(requests):
        summary["credits_after"] = _credits(provider, store, runner=runner, now=_now(clock))
        rows = _prediction_rows(store)
        main = [r for r in rows if r["variant"] == "main"]
        files = {
            "predictions.jsonl": store.write_jsonl("predictions.jsonl", main),
            "predictions.parquet": store.write_parquet("predictions.parquet", main),
            "paired_anonymized.parquet": store.write_parquet(
                "paired_anonymized.parquet", paired_rows(main, [r for r in rows if r["variant"] == "anonymized"])),
            "drift.parquet": store.write_parquet(
                "drift.parquet", paired_rows(main, [r for r in rows if r["variant"] == "drift"])),
        }
        _stage(store, "run", {**summary, "budget": checked["budget"]}, files)
    return summary


def report(store: RunStore) -> dict:
    stage = _verify_stage(store, "run")
    _verify_stage(store, "outcomes")
    manifest = store.read_json("manifest.json")
    result = build_report(manifest, _prediction_rows(store), store.read_jsonl("outcomes.jsonl"),
                          planned=len(store.read_jsonl("requests.jsonl")), budget=stage["budget"])
    # The numbers may be computed by later code than the inputs were built by
    # (a report can be recomputed); which code is recorded, not enforced.
    result["report_code"] = code_version()
    store.write_json("report.json", result)
    store.write_bytes("report.md", render_markdown(result).encode("utf-8"))
    return result


# ----------------------------------------------------------------- Phase B: the day, the status, the report


def phase_b_day(root: Path, cohort_id: str, s0: date | None, *, send: bool, budget: Budget | None = None,
                clock: Clock | None = None, yahoo=None, fetch_issues=None, yanoshin=None, transport=None,
                pacer: Pacer | None = None, sleep=time.sleep) -> dict:
    """One business day, in order: plan-day, build, preflight and - only with ``send`` - run.

    ``s0=None`` takes the day whose window is open now. A stage already done
    is not repeated; a day already sent is left alone.
    """

    now = _now(clock)
    if s0 is None:
        slot = phase_b.schedule(now)
        if not slot["window_open"]:
            return {"scheduled": slot, "done": "nothing: no S0's window is open now"}
        s0 = date.fromisoformat(slot["s0"])
    cohort = load_cohort(root, cohort_id)
    budget = budget or Budget(Decimal(cohort["budget"]["daily_budget_usd"]), cohort["budget"]["daily_max_requests"])
    store = phase_b.day_store(root, cohort_id, s0)
    result: dict = {"s0": s0.isoformat(), "run_id": store.run_id}
    if not store.exists("stage-plan.json"):
        result["plan"] = plan_day(root, cohort_id, s0, clock=clock, yahoo=yahoo, fetch_issues=fetch_issues,
                                  sleep=sleep)
    if not store.exists("stage-build.json"):
        result["build"] = build(store, yanoshin=yanoshin)
    if store.exists("stage-run.json"):
        result["already_sent"] = True
        return result
    if store.exists("run-stopped-1.json"):
        result["stopped"] = _latest(store, "run-stopped")
        return result
    checked = preflight(store, budget=budget, clock=clock)
    result["preflight"] = {k: checked[k] for k in ("ready_to_send", "samples", "requests", "requests_total",
                                                    "limit_problems", "budget_problems", "window_problems",
                                                    "global_cap", "estimated_usd_total")}
    result["preflight"]["violations"] = checked["leakage_and_integrity"]["samples_with_violations"]
    if send:
        result["run"] = (run(store, budget=budget, transport=transport, pacer=pacer, clock=clock)
                         if checked["ready_to_send"] else "not sent: the preflight is not ready")
    return result


def _day_status(store: RunStore) -> dict:
    samples = {r["sample_id"]: r["cohort"] for r in store.read_jsonl("population.jsonl")} \
        if store.exists("population.jsonl") else {}
    answered: Counter = Counter()
    if (store.path / "responses").exists():
        for path in (store.path / "responses").glob("*.json"):
            if ".raw" in path.name:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            sample_id, variant = record["label"].rsplit(".", 1)
            if record.get("status") == "ok":
                answered[samples.get(sample_id) if variant == "main" else variant] += 1
    preflight_latest = _latest(store, "preflight")
    return {
        "planned": store.exists("stage-plan.json"),
        "built": store.exists("stage-build.json"),
        "ready_to_send": bool(preflight_latest and preflight_latest["ready_to_send"]),
        "sent": store.exists("stage-run.json"),
        "stopped": _latest(store, "run-stopped"),
        "outcomes_frozen": store.exists("stage-outcomes.json"),
        "requests_planned": len(store.read_jsonl("requests.jsonl")) if store.exists("requests.jsonl") else 0,
        "answered": dict(answered),
        "spent_usd": str(phase_b.day_spend(store)),
    }


def phase_b_status(root: Path, cohort_id: str, *, clock: Clock | None = None) -> dict:
    store = phase_b.cohort_store(root, cohort_id)
    if not store.exists("cohort.json"):
        raise EvaluationError(f"no Phase B cohort {cohort_id!r}")
    cohort = store.read_json("cohort.json")
    try:
        load_cohort(root, cohort_id, allow_stopped=True)
        frozen = "holds"
    except EvaluationError as exc:
        frozen = str(exc)
    days = {day.isoformat(): _day_status(phase_b.day_store(root, cohort_id, day))
            for day in phase_b.day_dates(root, cohort_id)}
    spent = phase_b.cohort_spend(root, cohort_id)
    cap = Decimal(cohort["budget"]["global_hard_cap_usd"])
    return {
        "cohort_id": cohort_id,
        "frozen_protocol": frozen,
        "stops": phase_b.cohort_stops(root, cohort_id),
        "days": days,
        "days_sent": sum(d["sent"] for d in days.values()),
        "target_business_days": cohort["target_business_days"],
        "answered": {k: sum(d["answered"].get(k, 0) for d in days.values())
                     for k in ("PRIMARY", "CONTROL", "anonymized", "drift")},
        "targets": cohort["targets"],
        "spent_usd": str(spent),
        "cap_usd": str(cap),
        "remaining_usd": str(cap - spent),
        "credit": direct_credit_state(root, now=_now(clock)),
        "schedule": phase_b.schedule(_now(clock)),
    }


def phase_b_report(root: Path, cohort_id: str, *, clock: Clock | None = None) -> dict:
    """The cohort's report so far: every day sent in full, before S1 could open, whose outcomes are frozen."""

    cohort = load_cohort(root, cohort_id, allow_stopped=True)
    store = phase_b.cohort_store(root, cohort_id)
    predictions, outcomes, planned = [], [], 0
    included, pending, excluded = [], [], []
    for day in phase_b.day_dates(root, cohort_id):
        day_run = phase_b.day_store(root, cohort_id, day)
        if not day_run.exists("stage-run.json"):
            excluded.append({"s0": day.isoformat(), "reason": "not sent in full (stopped or not run)"})
            continue
        _verify_stage(day_run, "run")
        if not day_run.exists("stage-outcomes.json"):
            pending.append(day.isoformat())
            continue
        _verify_stage(day_run, "outcomes")
        rows = _prediction_rows(day_run)
        deadline = phase_b.send_deadline(day)
        late = [r for r in rows if r["sent_at"] and datetime.fromisoformat(r["sent_at"]) >= deadline]
        if late:
            excluded.append({"s0": day.isoformat(), "reason": f"{len(late)} answers sent after S1 could open"})
            continue
        predictions += rows
        outcomes += day_run.read_jsonl("outcomes.jsonl")
        planned += len(day_run.read_jsonl("requests.jsonl"))
        included.append(day.isoformat())
    result = build_report({"run_id": cohort_id, "phase": "B", "model": cohort["requested_model"]}, predictions,
                          outcomes, planned=planned, budget=cohort["budget"])
    result["cohort"] = {
        "cohort_id": cohort_id, "frozen_fingerprint": cohort["frozen_fingerprint"],
        "experiment_seed": cohort["experiment_seed"], "prospective_start": cohort["prospective_start"],
        "days_included": included, "days_pending_outcomes": pending, "days_excluded": excluded,
        "stops": phase_b.cohort_stops(root, cohort_id), "spent_usd": str(phase_b.cohort_spend(root, cohort_id)),
        "cap_usd": cohort["budget"]["global_hard_cap_usd"], "reported_at": _now(clock).isoformat(),
    }
    result["report_code"] = code_version()
    name = _next_name(store, "report")
    store.write_json(name, result)
    store.write_bytes(name.replace(".json", ".md"), render_markdown(result).encode("utf-8"))
    return result


# ----------------------------------------------------------------- CLI


def _day(text: str) -> date | None:
    return None if text == "auto" else date.fromisoformat(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="evaluation root (default: SURGE_EVALUATION_ROOT)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build", "freeze-outcomes", "preflight", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--run-id", required=True, help="a Phase A run, or a Phase B day as <cohort>/days/<S0>")
        if name == "plan":
            p.add_argument("--phase", default="A", choices=sorted(PHASES))
            p.add_argument("--seed", type=int, required=True)
            p.add_argument("--symbols", type=int, default=400)
            p.add_argument("--per-month", type=int, default=2)
        if name in ("preflight", "run"):
            p.add_argument("--budget-usd", type=Decimal, required=True)
            p.add_argument("--max-requests", type=int, required=True)
            p.add_argument("--runner", type=Path, default=RUNNER)
            p.add_argument("--provider", choices=PROVIDER_NAMES, default=None,
                           help="the manifest's provider unless given; vercel-ai-gateway is Phase A's fallback")
        if name == "preflight":
            p.add_argument("--no-credits", action="store_true", help="skip the Gateway balance (not ready to send)")
    p = sub.add_parser("phase-b-init")
    p.add_argument("--cohort-id", required=True)
    p.add_argument("--experiment-seed", required=True)
    p = sub.add_parser("record-credit", help="one reading of console.typesafe.ai/settings/billing")
    p.add_argument("--balance-usd", type=Decimal, required=True, help="Credit Balance as shown")
    p.add_argument("--confirmed-at", type=datetime.fromisoformat, required=True,
                   help="when it was read (ISO 8601 with timezone)")
    p.add_argument("--expires-on", type=date.fromisoformat, required=True,
                   help="the credit's expiry date as shown (Expires (UTC)); taken as its start, UTC")
    p.add_argument("--displayed-expiry", default=None, help="the expiry text as shown, e.g. 'Oct 19, 2026'")
    for name in ("plan-day", "phase-b-day"):
        p = sub.add_parser(name)
        p.add_argument("--cohort-id", required=True)
        p.add_argument("--s0", type=_day, required=name == "plan-day", default=None,
                       help="the S0 session (YYYY-MM-DD); phase-b-day also takes 'auto', the day whose window is open")
        if name == "phase-b-day":
            p.add_argument("--send", action="store_true", help="send the day's requests (without it: preflight only)")
            p.add_argument("--budget-usd", type=Decimal, default=None)
            p.add_argument("--max-requests", type=int, default=None)
    for name in ("phase-b-status", "phase-b-report"):
        p = sub.add_parser(name)
        p.add_argument("--cohort-id", required=True)
    args = parser.parse_args(argv)
    root = args.root or default_root()

    if args.command == "phase-b-init":
        result = phase_b_init(root, args.cohort_id, experiment_seed=args.experiment_seed)
    elif args.command == "record-credit":
        result = record_credit(root, balance_usd=args.balance_usd, confirmed_at=args.confirmed_at,
                               expires_on=args.expires_on, displayed_expiry=args.displayed_expiry)
    elif args.command == "plan-day":
        if args.s0 is None:
            parser.error("plan-day needs an explicit --s0")
        result = plan_day(root, args.cohort_id, args.s0)
    elif args.command == "phase-b-day":
        budget = None if args.budget_usd is None else Budget(args.budget_usd, args.max_requests or MAX_REQUESTS_PER_DAY)
        result = phase_b_day(root, args.cohort_id, args.s0, send=args.send, budget=budget)
    elif args.command == "phase-b-status":
        result = phase_b_status(root, args.cohort_id)
    elif args.command == "phase-b-report":
        result = phase_b_report(root, args.cohort_id)
    else:
        store = RunStore(root, args.run_id)
        if args.command == "plan":
            result = plan(store, phase=args.phase, seed=args.seed, symbols=args.symbols, per_month=args.per_month)
        elif args.command == "build":
            result = build(store)
        elif args.command == "freeze-outcomes":
            result = freeze_outcomes(store)
        elif args.command == "preflight":
            result = preflight(store, budget=Budget(args.budget_usd, args.max_requests),
                               runner=None if args.no_credits else args.runner, provider=args.provider)
        elif args.command == "run":
            result = run(store, budget=Budget(args.budget_usd, args.max_requests), runner=args.runner,
                         provider=args.provider)
        else:
            result = report(store)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
