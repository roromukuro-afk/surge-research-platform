"""`python -m surge.jobs.jev_smoke` - can Jev return the prediction's typed decisions?

A fit smoke, not the pipeline, and imported by nothing in it. One real
security, one real S0:

    build        --code 4755 --s0 2026-09-17 --out DIR   assemble the state, save the exact request
    gateway-send --out DIR --label run1 --runner R       one request through Vercel AI Gateway (D-269)
    send         --out DIR --label run1                  one request to TypeSafe's own API (kept for
                                                          a later comparison; early access only)
    compare      --out DIR                               how far the repeated answers drifted

``build`` makes no model request; each ``*send`` makes exactly one, with the
bytes ``build`` saved, so a repeat is the identical input. ``R`` is
``ops/jev-gateway-runner/runner.mjs`` (evaluation is AI SDK only). Nothing is
written to the database; the records go to ``DIR`` only, outside the
repository, because the answers are about a real security (CLAUDE.md:
prediction data is never committed).

The state is the method in full - Canonical v5.1 and every addendum, not
shortened (CLAUDE.md 1-2) - plus real data for S0: the confirmed close
(D-262), daily bars up to S0 from the same Yahoo source, and TDnet disclosure
*titles* up to the data cutoff (the index only; bodies are not fetched, D-126).
What code decides - the target, the 3,000 yen filter, the horizon - is in the
state as given facts and is never asked (``surge.analysis.jev_questions``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from surge.analysis.jev_questions import (
    ENDPOINT,
    MODEL,
    USD_PER_MILLION_INPUT_TOKENS,
    completeness,
    questions,
)

ENV_KEY = "TYPESAFE_API_KEY"
#: A month of sessions. The current prediction bundle carries features rather
#: than raw bars; a month keeps the state near the current prompt's size.
BARS = 20
DISCLOSURE_DAYS = 60
#: TypeSafe's Privacy Policy (last updated 2025-11-19): "We will not train or
#: fine tune any artificial intelligence or machine learning models on your
#: prompts or other Input." Zero data retention is offered to enterprise
#: customers only, so the production privacy gate (ZDR confirmed on the
#: account) does not pass; a one-off experiment is what not-trained-on allows.
PRIVACY_BASIS = "NOT_USED_FOR_TRAINING (typesafe.ai/legal/privacy-policy); ZDR enterprise-only: experiment, not production"


def build(code: str, s0: date, out: Path) -> dict:
    from surge.entry.eod_prediction import CHECKPOINT_SESSIONS, eod_prediction_terms
    from surge.jobs.analysis_feasibility import CANONICAL_PATH, _addenda_texts
    from surge.news.sources.yanoshin_tdnet import YanoshinTdnetSource
    from surge.providers.yahoo_finance import JST, YahooSession, session_close, tse_symbol

    symbol = tse_symbol(code)
    yahoo = YahooSession()
    close = session_close(symbol, s0, session=yahoo)
    # The evening of S0: after the close was confirmed, before S1 opens.
    data_cutoff = datetime(s0.year, s0.month, s0.day, tzinfo=JST) + timedelta(days=1)
    terms = eod_prediction_terms(
        close, security_id=f"smoke:{symbol}", data_cutoff=data_cutoff.astimezone(UTC),
        decision_completed_at=max(datetime.now(UTC), data_cutoff.astimezone(UTC)),
    )

    start = datetime(s0.year, s0.month, s0.day, tzinfo=JST) - timedelta(days=120)
    chart = yahoo.chart(symbol, {"interval": "1d", "period1": str(int(start.timestamp())),
                                 "period2": str(int(data_cutoff.timestamp()))})
    result = chart["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    bars = []
    for i, stamp in enumerate(result.get("timestamp") or []):
        day = datetime.fromtimestamp(stamp, JST).date()
        if day > s0 or quote["close"][i] is None:
            continue
        bars.append({"date": day.isoformat(), "open": quote["open"][i], "high": quote["high"][i],
                     "low": quote["low"][i], "close": quote["close"][i], "volume": quote["volume"][i]})
    bars = bars[-BARS:]

    tdnet = YanoshinTdnetSource().fetch_for_codes([code])
    since = data_cutoff - timedelta(days=DISCLOSURE_DAYS)
    disclosures = [
        {"published_at": item.pubdate.isoformat(), "title": item.title}
        for item in sorted(tdnet.items, key=lambda it: it.pubdate)
        if since <= item.pubdate.astimezone(JST) <= data_cutoff
    ]

    state = {
        "method": {
            "canonical_v5_1": CANONICAL_PATH.read_text(encoding="utf-8"),
            "addenda_newer_overrides_older": _addenda_texts(),
        },
        "security": {"market": "JP", "code": code, "yahoo_symbol": symbol},
        "s0": {
            "session_date": s0.isoformat(),
            "confirmed_close_jpy": str(close.close),
            "close_read_at": close.fetched_at.isoformat(),
        },
        "given_by_code_not_to_be_judged": {
            "target_price_jpy": str(terms.target_price),
            "price_limit_3000_yen_passed": True,
            "evaluation_starts": "S1 (the next session)",
            "checkpoints": [f"T+{n}" for n in CHECKPOINT_SESSIONS],
            "deadline": "T+20",
        },
        "data_cutoff": data_cutoff.isoformat(),
        "daily_bars_up_to_s0": bars,
        "tdnet_disclosure_titles_up_to_cutoff": disclosures,
    }
    body = {"state": state, "model": MODEL, "questions": questions()}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    (out / "request.json").write_bytes(raw)

    summary = {
        "built_at": datetime.now(UTC).isoformat(),
        "security": symbol,
        "s0": s0.isoformat(),
        "bars": len(bars),
        "first_bar": bars[0]["date"] if bars else None,
        "disclosure_titles": len(disclosures),
        "request_bytes": len(raw),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "state_o200k_tokens": _o200k(json.dumps(state, ensure_ascii=False)),
        "questions": {k: q["type"] for k, q in body["questions"].items()},
        "privacy_basis": PRIVACY_BASIS,
    }
    (out / "build.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _o200k(text: str) -> int | None:
    from surge.analysis.tokenizer import exact_tokens_or_none

    return exact_tokens_or_none(text, encoding="o200k_harmony")


def send(out: Path, label: str) -> dict:
    key = (os.environ.get(ENV_KEY) or "").strip()
    if not key:
        raise SystemExit(f"{ENV_KEY} is not in this process's environment; run through Invoke-WithSurgeSecrets.ps1")
    raw = (out / "request.json").read_bytes()
    asked = json.loads(raw)["questions"]
    request = urllib.request.Request(
        ENDPOINT, data=raw, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json; charset=utf-8",
                 "Accept": "application/json", "User-Agent": "surge-jev-fit-smoke"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - a fixed https endpoint
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    latency = time.perf_counter() - started
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        parsed = {"unparsed_bytes": len(body)}
    usage = parsed.get("usage") or {}
    record = {
        "label": label,
        "sent_at": datetime.now(UTC).isoformat(),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "status": status,
        "latency_seconds": round(latency, 3),
        "model": parsed.get("model"),
        "usage": usage,
        "cost_usd": (usage.get("input_tokens") or 0) / 1e6 * USD_PER_MILLION_INPUT_TOKENS,
        "answers": parsed.get("answers"),
        "error": None if status == 200 else parsed,
        "completeness_problems": completeness(asked, parsed) if status == 200 else ["no answers"],
    }
    (out / f"response-{label}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def gateway_send(out: Path, label: str, runner: Path) -> dict:
    """Send the saved request once through Vercel AI Gateway (``typesafe-ai/jev``).

    Evaluation is available through the AI SDK only
    (https://vercel.com/docs/ai-gateway/modalities/evaluation), so a small Node
    runner makes the call; this side converts the questions to the Gateway's
    shape, and the answers back, with ``jev_questions.to_gateway`` /
    ``from_gateway``. The runner needs AI_GATEWAY_API_KEY in the environment
    (run through Invoke-WithSurgeSecrets.ps1) and records no request.
    """

    import subprocess

    from surge.analysis.jev_questions import GATEWAY_MODEL, to_gateway

    body = json.loads((out / "request.json").read_bytes())
    gateway_input = out / "gateway-request.json"
    if not gateway_input.exists():
        converted = {"model": GATEWAY_MODEL, "state": body["state"], "questions": to_gateway(body["questions"])}
        gateway_input.write_text(json.dumps(converted, ensure_ascii=False), encoding="utf-8")
    raw_out = out / f"gateway-raw-{label}.json"
    subprocess.run(["node", str(runner), str(gateway_input), str(raw_out)], check=False, timeout=300,
                   cwd=str(runner.parent), capture_output=True, text=True)
    record = gateway_record(
        json.loads(raw_out.read_text(encoding="utf-8")),
        asked=body["questions"],
        label=label,
        request_sha256=hashlib.sha256(gateway_input.read_bytes()).hexdigest(),
    )
    (out / f"response-gw-{label}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


def gateway_record(result: dict, *, asked: dict, label: str, request_sha256: str) -> dict:
    """What is kept of one Gateway evaluation: answers, usage, cost, routing - no request."""

    from surge.analysis.jev_questions import from_gateway

    metadata = result.get("providerMetadata") or {}
    gateway = metadata.get("gateway") or {}
    routing = gateway.get("routing") or {}
    attempts = [a for m in routing.get("modelAttempts") or [] for a in m.get("providerAttempts") or []]
    ok_attempt = next((a for a in attempts if a.get("success")), None)
    answers = from_gateway(result.get("answers") or {}, (metadata.get("typesafe") or {}).get("confidence"))
    return {
        "label": label,
        "via": "vercel-ai-gateway",
        "sent_at": datetime.now(UTC).isoformat(),
        "request_sha256": request_sha256,
        "status": "error" if "error" in result else "ok",
        "error": result.get("error"),
        "latency_seconds": round(result.get("latencyMs", 0) / 1000, 3),
        "provider_attempt_seconds": (
            round((ok_attempt["endTime"] - ok_attempt["startTime"]) / 1000, 3) if ok_attempt else None
        ),
        "model": (result.get("response") or {}).get("modelId"),
        "resolved_provider": routing.get("finalProvider"),
        "provider_attempts": len(attempts),
        "generation_id": gateway.get("generationId"),
        "usage": result.get("usage"),
        "gateway_cost_usd": gateway.get("cost"),
        "typesafe_metadata": metadata.get("typesafe"),
        "answers": answers,
        "completeness_problems": (
            completeness(asked, {"answers": answers}, require_confidence=False)
            if "error" not in result else ["no answers"]
        ),
    }


def compare(out: Path) -> dict:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("response-*.json"))]
    runs = [r for r in runs if r.get("status") in (200, "ok")]
    if len(runs) < 2:
        return {"runs": len(runs), "note": "fewer than two successful runs; nothing to compare"}
    first, rest = runs[0]["answers"], [r["answers"] for r in runs[1:]]
    drift = {"runs": len(runs), "same_request": len({r["request_sha256"] for r in runs}) == 1}
    drift["decision_choices"] = [r["answers"]["decision"]["choice"] for r in runs]
    worst = {}
    for key, answer in first.items():
        deltas = []
        for other in rest:
            b = other.get(key) or {}
            if answer.get("type") == "noul":
                deltas.append(abs(answer["noul"] - b.get("noul", 0)))
            else:
                probs_a, probs_b = answer.get("probabilities") or {}, b.get("probabilities") or {}
                deltas.append(max((abs(probs_a[k] - probs_b.get(k, 0)) for k in probs_a), default=0))
                if answer.get("type") == "score":
                    deltas.append(abs(answer["score"] - b.get("score", 0)))
        worst[key] = round(max(deltas), 4)
    drift["max_abs_change_per_question"] = worst
    (out / "drift.json").write_text(json.dumps(drift, indent=2, ensure_ascii=False), encoding="utf-8")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--code", required=True)
    b.add_argument("--s0", required=True, type=date.fromisoformat)
    b.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("send")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--label", required=True)
    g = sub.add_parser("gateway-send")
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--label", required=True)
    g.add_argument("--runner", required=True, type=Path, help="the Node runner calling experimental_evaluate")
    c = sub.add_parser("compare")
    c.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "build":
        result = build(args.code, args.s0, args.out)
    elif args.command == "send":
        result = send(args.out, args.label)
    elif args.command == "gateway-send":
        result = gateway_send(args.out, args.label, args.runner)
    else:
        result = compare(args.out)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
