"""The evaluation's numbers: prediction rows, report.json and report.md (``eval-report-1.0.0``).

Cohorts are never pooled (D-270). **Primary** carries the evaluation - Brier
score and its skill over the base rate, calibration, PR-AUC (average
precision), hit rates by decision, and whether the upside score orders the
future return - computed separately for the two +20% definitions, neither of
which is the label (D-268). **Control** is a benchmark only: its hit rates
beside Primary's, to see whether screening followed by Jev adds anything; no
calibration is computed for it.

The anonymized and repeated requests are compared pairwise with their main
request and kept out of every number above.

A Phase A report says first that its numbers are not for adoption.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable

from surge.analysis.jev_questions import BASIS, DECISIONS

REPORT_VERSION = "eval-report-1.0.0"
HIT_DEFINITIONS = ("hit_20_high", "hit_20_close")
BASIS_KEYS = [f"basis_{kind.value.lower()}" for kind in BASIS]
PHASE_A_BANNER = (
    "Phase A retrospective pilot: pipeline verification only. Jev may have seen these outcomes in training; "
    "nothing in this report is used to adopt or reject it (D-270)."
)
VARIANTS = ("main", "anonymized", "drift")


# ----------------------------------------------------------------- the rows


def prediction_row(record: dict, sample: dict, *, variant: str, request: dict, evaluation_version: str,
                   question_schema_hash: str) -> dict:
    """One answered (or failed) request, flattened. ``record`` is ``jev_smoke.gateway_record``'s shape."""

    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    answers = record.get("answers") or {}
    decision = answers.get("decision") or {}
    upside = answers.get("upside_band") or {}
    usage = record.get("usage") or {}
    # TypeSafe direct records carry cost_usd (published price x tokens); Gateway
    # records carry the reported gateway_cost_usd (and, since D-275, cost_usd too).
    cost = record.get("cost_usd", record.get("gateway_cost_usd"))
    gateway_cost = record.get("gateway_cost_usd")
    problems = list(record.get("completeness_problems") or [])
    return {
        "sample_id": sample["sample_id"],
        "cohort": sample["cohort"],
        "variant": variant,
        "code": sample["code"],
        "s0": sample["s0"],
        "request_file": request["file"],
        "request_sha256": request["sha256"],
        "o200k_tokens": request["o200k_tokens"],
        "estimated_usd": float(request["estimated_usd"]),
        "sent_at": record.get("sent_at"),
        "status": record.get("status"),
        "error": record.get("error"),
        "latency_seconds": record.get("latency_seconds"),
        "provider_attempt_seconds": record.get("provider_attempt_seconds"),
        "served_model": record.get("model"),
        "resolved_provider": record.get("resolved_provider"),
        "provider_attempts": record.get("provider_attempts"),
        "generation_id": record.get("generation_id"),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "via": record.get("via"),
        "request_id": record.get("request_id") or record.get("generation_id"),
        "wire_sha256": record.get("wire_sha256"),
        "cost_usd": None if cost is None else float(cost),
        "cost_basis": record.get("cost_basis") or ("reported by the Gateway" if gateway_cost is not None else None),
        "gateway_cost_usd": None if gateway_cost is None else float(gateway_cost),
        # Every answer as recorded (for TypeSafe direct, exactly as the API returned it).
        "answers_as_recorded": answers,
        "rate_limited_attempts": len(record.get("rate_limited_attempts") or []),
        "decision": decision.get("choice"),
        "decision_probabilities": decision.get("probabilities"),
        "decision_confidence": decision.get("confidence"),
        "reaches_target": (answers.get("reaches_target") or {}).get("noul"),
        "upside_score": upside.get("score"),
        "upside_probabilities": upside.get("probabilities"),
        "upside_confidence": upside.get("confidence"),
        **{key: (answers.get(key) or {}).get("noul") for key in BASIS_KEYS},
        "completeness_problems": problems,
        "complete": record.get("status") == "ok" and not problems,
        "evaluation_version": evaluation_version,
        "question_schema_hash": question_schema_hash,
        "teacher_admissible": False,
    }


# ----------------------------------------------------------------- metrics


def brier(probabilities: list[float], hits: list[bool]) -> float:
    return sum((p - (1.0 if h else 0.0)) ** 2 for p, h in zip(probabilities, hits, strict=True)) / len(hits)


def average_precision(scores: list[float], hits: list[bool]) -> float | None:
    """Area under the precision-recall curve, step-wise; tied scores are one threshold."""

    positives = sum(hits)
    if positives == 0:
        return None
    ordered = sorted(zip(scores, hits, strict=True), key=lambda pair: -pair[0])
    area, true_pos, false_pos, last_recall, i = 0.0, 0, 0, 0.0, 0
    while i < len(ordered):
        threshold = ordered[i][0]
        while i < len(ordered) and ordered[i][0] == threshold:
            true_pos, false_pos = true_pos + ordered[i][1], false_pos + (not ordered[i][1])
            i += 1
        recall = true_pos / positives
        area += (recall - last_recall) * (true_pos / (true_pos + false_pos))
        last_recall = recall
    return area


def calibration(probabilities: list[float], hits: list[bool], bins: int = 10) -> dict:
    table = []
    for b in range(bins):
        members = [(p, h) for p, h in zip(probabilities, hits, strict=True) if min(int(p * bins), bins - 1) == b]
        if members:
            table.append({
                "bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}",
                "n": len(members),
                "mean_predicted": sum(p for p, _ in members) / len(members),
                "observed_rate": sum(h for _, h in members) / len(members),
            })
    n = len(hits)
    ece = sum(row["n"] * abs(row["mean_predicted"] - row["observed_rate"]) for row in table) / n if n else None
    return {"bins": table, "expected_calibration_error": ece}


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: Iterable[float | None], ys: Iterable[float | None]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    rx, ry = _ranks([x for x, _ in pairs]), _ranks([y for _, y in pairs])
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sxx, syy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    return None if sxx == 0 or syy == 0 else sxy / math.sqrt(sxx * syy)


def _mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return statistics.fmean(known) if known else None


def _median(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def _rate(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def by_decision(rows: list[dict]) -> dict:
    table = {}
    for choice in DECISIONS:
        members = [r for r in rows if r["decision"] == choice]
        table[choice] = {
            "n": len(members),
            **{f"{d}_rate": _rate([bool(r[d]) for r in members]) for d in HIT_DEFINITIONS},
            "mean_ret_t20": _mean(r["ret_t20"] for r in members),
            "median_max_upside_high": _median(r["max_upside_high"] for r in members),
            "median_max_drawdown_low": _median(r["max_drawdown_low"] for r in members),
        }
    return table


def primary_metrics(rows: list[dict]) -> dict:
    """``rows``: Primary, main variant, complete answer, RESOLVED outcome - joined."""

    result: dict = {"n": len(rows)}
    if not rows:
        return result
    probabilities = [float(r["reaches_target"]) for r in rows]
    for definition in HIT_DEFINITIONS:
        hits = [bool(r[definition]) for r in rows]
        base = sum(hits) / len(hits)
        score = brier(probabilities, hits)
        reference = base * (1 - base)
        result[definition] = {
            "positives": sum(hits),
            "base_rate": base,
            "brier": score,
            "brier_reference_base_rate": reference,
            "brier_skill": None if reference == 0 else 1 - score / reference,
            "pr_auc_average_precision": average_precision(probabilities, hits),
            "calibration": calibration(probabilities, hits),
        }
    result["by_decision"] = by_decision(rows)
    upside = [r["upside_score"] for r in rows]
    result["score_vs_future_return_spearman"] = {
        "upside_score_vs_max_upside_high": spearman(upside, [r["max_upside_high"] for r in rows]),
        "upside_score_vs_max_upside_close": spearman(upside, [r["max_upside_close"] for r in rows]),
        "upside_score_vs_ret_t20": spearman(upside, [r["ret_t20"] for r in rows]),
        "reaches_target_vs_max_upside_high": spearman(probabilities, [r["max_upside_high"] for r in rows]),
    }
    return result


def control_benchmark(control: list[dict], primary: list[dict]) -> dict:
    """Control beside Primary: hit rates only, never pooled, no calibration."""

    def entry_rate(rows: list[dict], definition: str) -> float | None:
        return _rate([bool(r[definition]) for r in rows if r["decision"] == "ENTRY"])

    return {
        "note": "benchmark only: not pooled with Primary, no calibration computed",
        "n": len(control),
        "decision_counts": {choice: sum(r["decision"] == choice for r in control) for choice in DECISIONS},
        "mean_reaches_target": _mean(r["reaches_target"] for r in control),
        "pipeline_value": {
            definition: {
                "primary_base_rate": _rate([bool(r[definition]) for r in primary]),
                "control_base_rate": _rate([bool(r[definition]) for r in control]),
                "primary_entry_hit_rate": entry_rate(primary, definition),
                "control_entry_hit_rate": entry_rate(control, definition),
            }
            for definition in HIT_DEFINITIONS
        },
    }


def paired_rows(main: list[dict], other: list[dict]) -> list[dict]:
    """Each anonymized or repeated answer beside its main answer."""

    by_id = {r["sample_id"]: r for r in main}
    rows = []
    for o in other:
        m = by_id.get(o["sample_id"])
        if m is None or not (m["complete"] and o["complete"]):
            rows.append({"sample_id": o["sample_id"], "cohort": o["cohort"], "variant": o["variant"],
                         "comparable": False})
            continue
        options = set(m["decision_probabilities"] or {}) | set(o["decision_probabilities"] or {})
        rows.append({
            "sample_id": o["sample_id"],
            "cohort": o["cohort"],
            "variant": o["variant"],
            "comparable": True,
            "same_request": m["request_sha256"] == o["request_sha256"],
            "decision_main": m["decision"],
            "decision_other": o["decision"],
            "decision_changed": m["decision"] != o["decision"],
            "decision_probability_max_abs_diff": max(
                (abs((m["decision_probabilities"] or {}).get(k, 0) - (o["decision_probabilities"] or {}).get(k, 0))
                 for k in options), default=0.0),
            "reaches_target_main": m["reaches_target"],
            "reaches_target_other": o["reaches_target"],
            "reaches_target_diff": o["reaches_target"] - m["reaches_target"],
            "upside_score_main": m["upside_score"],
            "upside_score_other": o["upside_score"],
            "upside_score_diff": o["upside_score"] - m["upside_score"],
            "basis_max_abs_diff": max(abs(o[k] - m[k]) for k in BASIS_KEYS),
            "teacher_admissible": False,
        })
    return rows


def paired_summary(rows: list[dict]) -> dict:
    comparable = [r for r in rows if r["comparable"]]
    return {
        "n": len(rows),
        "comparable": len(comparable),
        "decision_changed": sum(r["decision_changed"] for r in comparable),
        "decision_change_rate": _rate([r["decision_changed"] for r in comparable]),
        "reaches_target_abs_diff_mean": _mean(abs(r["reaches_target_diff"]) for r in comparable),
        "reaches_target_abs_diff_max": max((abs(r["reaches_target_diff"]) for r in comparable), default=None),
        "upside_score_abs_diff_mean": _mean(abs(r["upside_score_diff"]) for r in comparable),
        "upside_score_abs_diff_max": max((abs(r["upside_score_diff"]) for r in comparable), default=None),
        "decision_probability_max_abs_diff_max": max(
            (r["decision_probability_max_abs_diff"] for r in comparable), default=None),
    }


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def pipeline_summary(predictions: list[dict], outcomes: list[dict], *, planned: int, budget: dict) -> dict:
    sent = [p for p in predictions if p["status"] is not None]
    latencies = [p["latency_seconds"] for p in sent if p["latency_seconds"] is not None]
    tokens = [p["input_tokens"] for p in sent if p["input_tokens"] is not None]
    ratios = [p["input_tokens"] / p["o200k_tokens"] for p in sent if p["input_tokens"] and p["o200k_tokens"]]
    costs = [p["cost_usd"] for p in sent if p.get("cost_usd") is not None]
    gateway_costs = [p["gateway_cost_usd"] for p in sent if p.get("gateway_cost_usd") is not None]
    return {
        "requests": {
            "planned": planned,
            "sent": len(sent),
            "ok": sum(p["status"] == "ok" for p in sent),
            "errors": sum(p["status"] != "ok" for p in sent),
            "incomplete": sum(p["status"] == "ok" and not p["complete"] for p in sent),
            "by_variant": {v: sum(p["variant"] == v for p in sent) for v in VARIANTS},
        },
        "cost": {
            "budget": budget,
            "cost_usd_total": sum(costs),
            "cost_bases": sorted({p["cost_basis"] for p in sent if p.get("cost_basis")}),
            "gateway_cost_usd_total": sum(gateway_costs),
            "requests_without_reported_cost": len(sent) - len(costs),
            "estimated_usd_total": sum(p["estimated_usd"] for p in sent),
            "cost_usd_mean": _mean(costs),
        },
        "providers": sorted({p["via"] for p in sent if p.get("via")}),
        "rate_limited_attempts": sum(p.get("rate_limited_attempts") or 0 for p in sent),
        "tokens": {
            "input_mean": _mean(tokens),
            "input_min": min(tokens, default=None),
            "input_p50": _quantile(tokens, 0.5),
            "input_p90": _quantile(tokens, 0.9),
            "input_max": max(tokens, default=None),
            "output_total": sum(p["output_tokens"] or 0 for p in sent),
            "jev_to_o200k_ratio_mean": _mean(ratios),
        },
        "latency_seconds": {"p50": _quantile(latencies, 0.5), "p90": _quantile(latencies, 0.9),
                            "max": max(latencies, default=None)},
        "served_models": sorted({p["served_model"] for p in sent if p["served_model"]}),
        "resolved_providers": sorted({p["resolved_provider"] for p in sent if p["resolved_provider"]}),
        "outcomes": {status: sum(o["resolution"] == status for o in outcomes)
                     for status in ("RESOLVED", "UNRESOLVED_MISSING_DATA", "PENDING")},
    }


def answers_summary(rows: list[dict]) -> dict:
    """What the answers look like, per cohort - counts and ranges, not a performance figure."""

    def spread(key: str) -> dict:
        values = sorted(r[key] for r in rows if r[key] is not None)
        return {"n": len(values), "min": values[0] if values else None, "median": _median(values),
                "max": values[-1] if values else None}

    return {
        "n": len(rows),
        "decision_counts": {choice: sum(r["decision"] == choice for r in rows) for choice in DECISIONS},
        "reaches_target": spread("reaches_target"),
        "upside_score": spread("upside_score"),
        "decision_confidence": spread("decision_confidence"),
    }


def build_report(manifest: dict, predictions: list[dict], outcomes: list[dict], *, planned: int,
                 budget: dict) -> dict:
    """``predictions``: every variant. The evaluation reads main answers joined to RESOLVED outcomes only.

    With few samples some numbers are undefined - no positives leave PR-AUC and
    Brier skill undefined, fewer than three pairs leave a rank correlation
    undefined - and they are reported as null, never filled in.
    """

    by_sample = {o["sample_id"]: o for o in outcomes}
    main = [p for p in predictions if p["variant"] == "main"]
    joined = [
        {**p, **{k: v for k, v in by_sample[p["sample_id"]].items() if k not in p}}
        for p in main
        if p["complete"] and p["sample_id"] in by_sample and by_sample[p["sample_id"]]["resolution"] == "RESOLVED"
    ]
    primary = [r for r in joined if r["cohort"] == "PRIMARY"]
    control = [r for r in joined if r["cohort"] == "CONTROL"]
    anonymized = paired_rows(main, [p for p in predictions if p["variant"] == "anonymized"])
    drift = paired_rows(main, [p for p in predictions if p["variant"] == "drift"])
    pipeline = pipeline_summary(predictions, outcomes, planned=planned, budget=budget)
    pipeline["outcome_join"] = {
        "main_predictions": len(main),
        "complete": sum(p["complete"] for p in main),
        "joined_to_resolved_outcome": len(joined),
        "without_outcome": sorted(p["sample_id"] for p in main if p["sample_id"] not in by_sample),
        "outcome_not_resolved": sorted(p["sample_id"] for p in main if p["sample_id"] in by_sample
                                       and by_sample[p["sample_id"]]["resolution"] != "RESOLVED"),
        "by_cohort": {"PRIMARY": len(primary), "CONTROL": len(control)},
    }
    return {
        "report_version": REPORT_VERSION,
        "run_id": manifest["run_id"],
        "phase": manifest["phase"],
        "banner": PHASE_A_BANNER if manifest["phase"] == "A" else None,
        "model": manifest["model"],
        "outcome_label_status": "neither +20% definition is the label (D-268); not merged with success_label",
        "undefined_values": "null (in markdown '-') where a statistic is undefined for these samples",
        "pipeline": pipeline,
        "answers": {"PRIMARY": answers_summary(primary), "CONTROL": answers_summary(control)},
        "primary": primary_metrics(primary),
        "control_benchmark": control_benchmark(control, primary),
        "anonymized_sensitivity": paired_summary(anonymized),
        "drift": paired_summary(drift),
        "teacher_admissible": False,
    }


# ----------------------------------------------------------------- markdown


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict) -> str:
    lines = [f"# Jev evaluation {report['run_id']} (Phase {report['phase']})", ""]
    if report["banner"]:
        lines += [f"> **{report['banner']}**", ""]
    pipe = report["pipeline"]
    req, cost, tok, lat = pipe["requests"], pipe["cost"], pipe["tokens"], pipe["latency_seconds"]
    lines += [
        "## Pipeline", "",
        f"- requests: planned {req['planned']}, sent {req['sent']}, ok {req['ok']}, errors {req['errors']}, "
        f"incomplete {req['incomplete']} (main {req['by_variant']['main']}, anonymized "
        f"{req['by_variant']['anonymized']}, drift {req['by_variant']['drift']})",
        f"- cost: ${_fmt(cost['cost_usd_total'], 6)} ({'; '.join(cost['cost_bases']) or '-'}; estimated "
        f"${_fmt(cost['estimated_usd_total'], 6)}; {cost['requests_without_reported_cost']} without a cost); "
        f"budget {json.dumps(cost['budget'])}; provider {', '.join(pipe['providers']) or '-'}; "
        f"429 waits {pipe['rate_limited_attempts']}",
        f"- input tokens: mean {_fmt(tok['input_mean'], 0)}, min {_fmt(tok['input_min'])}, p50 "
        f"{_fmt(tok['input_p50'])}, p90 {_fmt(tok['input_p90'])}, max {_fmt(tok['input_max'])}; Jev / o200k "
        f"{_fmt(tok['jev_to_o200k_ratio_mean'])}",
        f"- latency: p50 {_fmt(lat['p50'])} s, p90 {_fmt(lat['p90'])} s, max {_fmt(lat['max'])} s",
        f"- served: {', '.join(pipe['served_models']) or '-'} via {', '.join(pipe['resolved_providers']) or '-'}",
        f"- outcomes: {json.dumps(pipe['outcomes'])}",
        f"- outcome join: {json.dumps(pipe['outcome_join'])}",
        "",
        "Values that are undefined for these samples are shown as '-'.",
        "",
        "## Answers (distribution only)",
        "",
        "| cohort | n | decisions | reaches_target min / median / max | upside score min / median / max |",
        "|---|---|---|---|---|",
    ]
    for cohort, a in report["answers"].items():
        rt, us = a["reaches_target"], a["upside_score"]
        decisions = ", ".join(f"{k} {v}" for k, v in a["decision_counts"].items() if v)
        lines.append(f"| {cohort} | {a['n']} | {decisions or '-'} | {_fmt(rt['min'])} / {_fmt(rt['median'])} / "
                     f"{_fmt(rt['max'])} | {_fmt(us['min'])} / {_fmt(us['median'])} / {_fmt(us['max'])} |")
    lines.append("")
    primary = report["primary"]
    lines += ["## Primary", "", f"n = {primary['n']}", ""]
    if primary["n"]:
        lines += ["| definition | positives | base rate | Brier | Brier skill | PR-AUC | ECE |",
                  "|---|---|---|---|---|---|---|"]
        for d in HIT_DEFINITIONS:
            m = primary[d]
            lines.append(f"| {d} | {m['positives']} | {_fmt(m['base_rate'])} | {_fmt(m['brier'])} | "
                         f"{_fmt(m['brier_skill'])} | {_fmt(m['pr_auc_average_precision'])} | "
                         f"{_fmt(m['calibration']['expected_calibration_error'])} |")
        lines += ["", "| decision | n | hit high | hit close | mean ret T+20 | median max upside |",
                  "|---|---|---|---|---|---|"]
        for choice, m in primary["by_decision"].items():
            lines.append(f"| {choice} | {m['n']} | {_fmt(m['hit_20_high_rate'])} | {_fmt(m['hit_20_close_rate'])} | "
                         f"{_fmt(m['mean_ret_t20'])} | {_fmt(m['median_max_upside_high'])} |")
        lines += ["", "Spearman: " + ", ".join(
            f"{k} {_fmt(v)}" for k, v in primary["score_vs_future_return_spearman"].items()), ""]
    control = report["control_benchmark"]
    lines += ["## Control (benchmark only)", "", f"n = {control['n']}; decisions {json.dumps(control['decision_counts'])}",
              ""]
    for d, v in control["pipeline_value"].items():
        lines.append(f"- {d}: base rate Primary {_fmt(v['primary_base_rate'])} / Control "
                     f"{_fmt(v['control_base_rate'])}; ENTRY hit rate Primary {_fmt(v['primary_entry_hit_rate'])} / "
                     f"Control {_fmt(v['control_entry_hit_rate'])}")
    for title, key in (("Anonymized sensitivity (paired, not in the evaluation)", "anonymized_sensitivity"),
                       ("Drift (identical input repeated)", "drift")):
        s = report[key]
        lines += ["", f"## {title}", "",
                  f"- pairs {s['n']} (comparable {s['comparable']}); decision changed {s['decision_changed']} "
                  f"(rate {_fmt(s['decision_change_rate'])})",
                  f"- reaches_target |diff| mean {_fmt(s['reaches_target_abs_diff_mean'])}, max "
                  f"{_fmt(s['reaches_target_abs_diff_max'])}; upside score |diff| mean "
                  f"{_fmt(s['upside_score_abs_diff_mean'])}, max {_fmt(s['upside_score_abs_diff_max'])}"]
    lines += ["", f"{report['outcome_label_status']}. All rows teacher_admissible = false.", ""]
    return "\n".join(lines)


__all__ = ["BASIS_KEYS", "HIT_DEFINITIONS", "PHASE_A_BANNER", "REPORT_VERSION", "answers_summary",
           "average_precision", "brier", "build_report", "by_decision", "calibration", "control_benchmark",
           "paired_rows", "paired_summary", "pipeline_summary", "prediction_row", "primary_metrics", "render_markdown",
           "spearman"]
