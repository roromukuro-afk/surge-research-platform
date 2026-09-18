"""`python -m surge.jobs.jev_eval` - the Jev evaluation run (docs/specs/jev-evaluation-design.md, D-270).

Each stage writes write-once files under ``<root>/evaluation/jev/<run_id>/``
(root: ``SURGE_EVALUATION_ROOT``, default ``~/.surge`` - outside the repository,
because the files hold market data and answers about real securities):

    plan            --run-id ID [--seed N] [--symbols N]    the population (reads JPX, Yahoo)
    build           --run-id ID                              states and the exact request bodies (reads Yanoshin)
    freeze-outcomes --run-id ID                              outcomes, fixed before any answer exists
    preflight       --run-id ID --budget-usd X --max-requests N [--runner R]
                                                             leakage checks, the budget, the Gateway credit balance
    run             --run-id ID --budget-usd X --max-requests N [--runner R]
                                                             the requests, inside the hard budget
    report          --run-id ID                              report.json and report.md

Only ``run`` sends model requests. ``preflight`` reads the Gateway credit
balance and nothing else. Nothing here writes to a database; every row is
``teacher_admissible = false``. Only Phase A (the retrospective pilot) can be
planned here; Phase B shadows the daily production screener and is not built
yet.

``R`` is ``ops/jev-gateway-runner/runner.mjs``; it needs AI_GATEWAY_API_KEY in
its environment (run through Invoke-WithSurgeSecrets.ps1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from surge.analysis.jev_questions import GATEWAY_MODEL, MODEL, questions, to_gateway
from surge.evaluation.cost import (
    CONTEXT_TOKENS,
    Budget,
    BudgetExceeded,
    Ledger,
    estimate_usd,
    estimated_jev_tokens,
    plan_problems,
    read_credits,
)
from surge.evaluation.leakage import check_sample
from surge.evaluation.method import REPO_ROOT, load_method
from surge.evaluation.outcome import OUTCOME_DEFINITION, OUTCOME_VERSION, compute_outcome
from surge.evaluation.population import (
    MIN_SPACING_SESSIONS,
    SCREENER_DEFINITION,
    Sample,
    choose_s0_dates,
    draw_population,
    screen,
)
from surge.evaluation.prices import (
    DATASET_KEY,
    TURNOVER_BASIS,
    History,
    PriceHistoryError,
    fetch_chart,
    history_from_chart,
    trading_sessions,
)
from surge.evaluation.report import (
    REPORT_VERSION,
    build_report,
    paired_rows,
    prediction_row,
    render_markdown,
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
from surge.features.engine import FEATURE_VERSION
from surge.routes.engine import ROUTE_VERSION

EVALUATION_VERSION = "jev-eval-1.0.0"
RUNNER = REPO_ROOT / "ops" / "jev-gateway-runner" / "runner.mjs"
#: The Gateway's routing names the provider that answered; for Jev it is TypeSafe's (D-269).
EXPECTED_PROVIDER = "typesafe-ai"
#: Seven months before the first S0: the feature warm-up (75 sessions) and the
#: 60-session highs need that much behind the earliest S0.
HISTORY_START = date(2024, 6, 1)
PHASES = {
    "A": {"s0_start": date(2025, 1, 1), "s0_end": date(2026, 6, 30),
          "primary": 75, "control": 25, "anonymized": 10, "drift": 5},
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
#: Everything that shapes an input, an outcome or a number. Their hashes are the
#: input-building code version, whether or not the tree is committed.
CODE_FILES = [
    "workers/src/surge/evaluation/__init__.py",
    "workers/src/surge/evaluation/cost.py",
    "workers/src/surge/evaluation/leakage.py",
    "workers/src/surge/evaluation/method.py",
    "workers/src/surge/evaluation/outcome.py",
    "workers/src/surge/evaluation/population.py",
    "workers/src/surge/evaluation/prices.py",
    "workers/src/surge/evaluation/report.py",
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
    "ops/jev-gateway-runner/package.json",
    "ops/jev-gateway-runner/package-lock.json",
]
YAHOO_PAUSE_SECONDS = 0.3


class EvaluationError(RuntimeError):
    pass


# ----------------------------------------------------------------- helpers


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
        "price_dataset": DATASET_KEY,
        "turnover_basis": TURNOVER_BASIS,
    }


def _runner_metadata() -> dict:
    package = json.loads((RUNNER.parent / "package.json").read_text(encoding="utf-8"))
    return {"ai_sdk": package["dependencies"]["ai"], "runner_sha256": hashlib.sha256(RUNNER.read_bytes()).hexdigest()}


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


def _load_histories(store: RunStore) -> dict[str, History]:
    histories = {}
    for path in sorted((store.path / "inputs" / "prices").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
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


# ----------------------------------------------------------------- plan


def plan(store: RunStore, *, phase: str = "A", seed: int, symbols: int, per_month: int = 2,
         now: datetime | None = None, yahoo=None, fetch_issues=None, sleep=time.sleep) -> dict:
    """Choose the securities, read their history, replay the screener at each S0, draw the samples.

    Everything is computed before anything is written, so a short population
    leaves no half-written run behind.
    """

    from surge.evaluation.universe import fetch_listed_issues
    from surge.providers.yahoo_finance import YahooError, YahooSession, tse_symbol

    if store.exists("manifest.json"):
        raise StoreError(f"run {store.run_id} has already been planned")
    if phase not in PHASES:
        raise EvaluationError(f"only Phase {sorted(PHASES)} can be planned here")
    params = PHASES[phase]
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
        symbol = tse_symbol(issue.code)
        for attempt in (1, 2):
            try:
                result = fetch_chart(symbol, HISTORY_START, session=yahoo, now=started)
                histories[issue.code] = history_from_chart(issue.code, symbol, result, fetched_at=started)
                charts[issue.code] = {"code": issue.code, "symbol": symbol, "fetched_at": started.isoformat(),
                                      "result": result}
                break
            except YahooError as exc:
                if attempt == 1:
                    # A lapsed cookie or crumb fails every later call too; start a new session once.
                    yahoo.reset()
                    sleep(2)
                    continue
                failures.append({"code": issue.code, "error": str(exc)[:200]})
            except PriceHistoryError as exc:
                failures.append({"code": issue.code, "error": str(exc)[:200]})
                break
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
            screening_rows.append({
                "code": code, "s0": day.isoformat(), "eligible": verdict.eligible, "passed": verdict.passed,
                "close_as_traded": None if verdict.close_as_traded is None else str(verdict.close_as_traded),
                "routes": verdict.routes, "turnover_avg_20d": verdict.turnover_avg_20d, "reason": verdict.reason,
            })
        screens_by_date[day] = slim

    samples = draw_population(
        screens_by_date, {i.code: i for i in chosen}, sessions, primary=params["primary"],
        control=params["control"], anonymized=params["anonymized"], drift=params["drift"], seed=seed,
    )
    counts = {c: sum(s.cohort == c for s in samples) for c in ("PRIMARY", "CONTROL")}
    if counts["PRIMARY"] < params["primary"] or counts["CONTROL"] < params["control"]:
        raise EvaluationError(f"the population is short: {counts} against {params['primary']} / {params['control']}; "
                              "plan again with more symbols")

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
    rows = [s.row() for s in samples]
    files["population.jsonl"] = store.write_jsonl("population.jsonl", rows)
    files["population.parquet"] = store.write_parquet("population.parquet", rows)

    manifest = {
        "run_id": store.run_id,
        "evaluation_version": EVALUATION_VERSION,
        "phase": phase,
        "started_at": started.isoformat(),
        "model": GATEWAY_MODEL,
        "provider": "vercel-ai-gateway",
        "model_version_metadata": {
            "requested_model": GATEWAY_MODEL,
            "upstream": "typesafe-ai (TypeSafe System One)",
            "typesafe_model_alias": MODEL,
            "served": "recorded per request (served_model, resolved_provider, generation_id in predictions.*) "
                      "and summarized in report.json; the Gateway id carries no version",
            **_runner_metadata(),
        },
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
        "samples": counts, "anonymized": sum(s.anonymized_pair for s in samples),
        "drift": sum(s.drift_repeat for s in samples),
    }
    _stage(store, "plan", summary, files)
    return summary


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
    samples, histories = _load_samples(store), _load_histories(store)
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


def freeze_outcomes(store: RunStore) -> dict:
    """Fix every outcome from prices before a single answer exists."""

    _verify_stage(store, "plan")
    if store.exists("stage-outcomes.json"):
        raise StoreError(f"outcomes for {store.run_id} are already frozen")
    _code_matches(store.read_json("manifest.json"))
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


# ----------------------------------------------------------------- preflight


def _next_name(store: RunStore, stem: str) -> str:
    n = 1
    while store.exists(f"{stem}-{n}.json"):
        n += 1
    return f"{stem}-{n}.json"


def preflight(store: RunStore, *, budget: Budget, runner: Path | None = RUNNER) -> dict:
    """Every check that can be made without sending: leakage, schema, budget, credit."""

    for stage in ("plan", "build", "outcomes"):
        _verify_stage(store, stage)
    manifest = store.read_json("manifest.json")
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
        problems = []
        bodies = {}
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
    credits = None
    if runner is not None:
        credits = read_credits(runner, store.path / _next_name(store, "credits"))
    budget_problems = plan_problems(budget, estimates, credits)
    tokens = [r["o200k_tokens"] for r in requests]
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": store.run_id,
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


# ----------------------------------------------------------------- run and report


def _latest(store: RunStore, stem: str) -> dict | None:
    n, latest = 1, None
    while store.exists(f"{stem}-{n}.json"):
        latest = store.read_json(f"{stem}-{n}.json")
        n += 1
    return latest


def _response_name(row: dict, raw: bool = False) -> str:
    return f"responses/{row['sample_id']}.{row['variant']}{'.raw' if raw else ''}.json"


def response_problems(record: dict, row: dict) -> list[str]:
    """Why the run must stop after this answer (D-270): any one of these ends it.

    A Gateway error; an incomplete answer; a model or provider other than
    Jev's; an answer recorded against another request; a cost the Gateway did
    not report, or above the request's (conservative) estimate; input tokens
    past the estimate.
    """

    if record.get("status") != "ok":
        return [f"Gateway error: {record.get('error')}"]
    problems = []
    if record.get("completeness_problems"):
        problems.append(f"schema incomplete: {record['completeness_problems']}")
    if record.get("model") != GATEWAY_MODEL:
        problems.append(f"unexpected model {record.get('model')!r}")
    if record.get("resolved_provider") != EXPECTED_PROVIDER:
        problems.append(f"unexpected provider {record.get('resolved_provider')!r}")
    if record.get("request_sha256") != row["sha256"]:
        problems.append("the answer is recorded against another request")
    cost = record.get("gateway_cost_usd")
    if cost is None:
        problems.append("the Gateway reported no cost")
    elif Decimal(str(cost)) > Decimal(row["estimated_usd"]):
        problems.append(f"cost ${cost} is above the request's estimate ${row['estimated_usd']}")
    tokens = (record.get("usage") or {}).get("inputTokens")
    if not isinstance(tokens, int) or not 0 < tokens <= row["estimated_jev_tokens"]:
        problems.append(f"input tokens {tokens!r} outside the estimate {row['estimated_jev_tokens']}")
    return problems


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


def run(store: RunStore, *, budget: Budget, runner: Path = RUNNER) -> dict:
    """Send the planned requests - main, then anonymized, then drift - inside the hard budget."""

    from surge.jobs.jev_smoke import gateway_record

    for stage in ("plan", "build", "outcomes"):
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
    manifest = store.read_json("manifest.json")
    _code_matches(manifest)
    _method_matches(manifest)

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
    credits = read_credits(runner, store.path / _next_name(store, "credits"))
    problems = plan_problems(budget, [Decimal(r["estimated_usd"]) for r in pending], credits)
    if problems:
        raise EvaluationError(f"not sending: {problems}")

    stopped = None
    asked = questions()
    for count, row in enumerate(pending, start=1):
        label = f"{row['sample_id']}.{row['variant']}"
        estimate = Decimal(row["estimated_usd"])
        try:
            ledger.check_next(estimate)
            # The bytes sent are the bytes built and checked, or nothing is sent.
            store.verify(row["file"], row["sha256"])
        except (BudgetExceeded, StoreError) as exc:
            stopped = f"{label}: {exc}"
            break
        raw_out = store.path / _response_name(row, raw=True)
        if raw_out.exists():
            raise StoreError(f"{raw_out} exists without its record; look at it before sending again")
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["node", str(runner), str(store.path / row["file"]), str(raw_out)], check=False,
                       timeout=300, cwd=str(runner.parent), capture_output=True, text=True)
        result = (json.loads(raw_out.read_text(encoding="utf-8")) if raw_out.exists()
                  else {"error": {"message": "the runner wrote nothing"}, "latencyMs": 0})
        record = gateway_record(result, asked=asked, label=label, request_sha256=row["sha256"])
        store.write_json(_response_name(row), record)
        ledger.record(record, estimate=estimate)
        problems = response_problems(record, row)
        if problems:
            stopped = f"{label}: {problems}"
            break
        if count % 10 == 0:
            _progress(f"sent {count}/{len(pending)}, ${ledger.spent_usd}")

    summary = {"sent_this_time": ledger.requests - (len(requests) - len(pending)), "requests_answered":
               ledger.requests, "spent_usd": str(ledger.spent_usd), "stopped": stopped,
               "estimated_charges": ledger.estimated_charges, "credits_before": credits}
    if stopped is not None:
        store.write_json(_next_name(store, "run-stopped"), summary)
    elif ledger.requests == len(requests):
        summary["credits_after"] = read_credits(runner, store.path / _next_name(store, "credits"))
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


# ----------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None, help="evaluation root (default: SURGE_EVALUATION_ROOT)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build", "freeze-outcomes", "preflight", "run", "report"):
        p = sub.add_parser(name)
        p.add_argument("--run-id", required=True)
        if name == "plan":
            p.add_argument("--phase", default="A", choices=sorted(PHASES))
            p.add_argument("--seed", type=int, required=True)
            p.add_argument("--symbols", type=int, default=400)
            p.add_argument("--per-month", type=int, default=2)
        if name in ("preflight", "run"):
            p.add_argument("--budget-usd", type=Decimal, required=True)
            p.add_argument("--max-requests", type=int, required=True)
            p.add_argument("--runner", type=Path, default=RUNNER)
        if name == "preflight":
            p.add_argument("--no-credits", action="store_true", help="skip the Gateway balance (not ready to send)")
    args = parser.parse_args(argv)
    store = RunStore(args.root or default_root(), args.run_id)

    if args.command == "plan":
        result = plan(store, phase=args.phase, seed=args.seed, symbols=args.symbols, per_month=args.per_month)
    elif args.command == "build":
        result = build(store)
    elif args.command == "freeze-outcomes":
        result = freeze_outcomes(store)
    elif args.command == "preflight":
        result = preflight(store, budget=Budget(args.budget_usd, args.max_requests),
                           runner=None if args.no_credits else args.runner)
    elif args.command == "run":
        result = run(store, budget=Budget(args.budget_usd, args.max_requests), runner=args.runner)
    else:
        result = report(store)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
