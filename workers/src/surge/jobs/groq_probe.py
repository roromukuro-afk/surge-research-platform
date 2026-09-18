"""`python -m surge.jobs.groq_probe` - what this Groq account actually allows.

Sends one request per model: a fixed two-word prompt, sixteen tokens of output,
no canonical method, no bundle, no security, no price, and built-in tools
refused. It exists to read the rate-limit headers that come back, because the
account's real limits are the only ones a preflight can honestly be run
against - a figure copied from a documentation page passes right up until the
day it does not.

Then it answers the question that decides whether this model can be used at
all: does the real minimum Entry request - Canonical v5.1 and the addenda in
full, plus the completion reserve - fit what was just measured? That is the same
preflight a production request goes through, run before any production request
is sent.

The result is written to the state directory, so the readiness report can say
"measured" from a record instead of being told so with a flag. The key is never
printed; nothing in the record could be used to call the API.

Run it with the credentials in the environment, which on this machine means
through ops\\windows\\Invoke-WithSurgeSecrets.ps1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from surge.analysis.groq_provider import (
    COMPOUND_MODEL_PREFIXES,
    DEFAULT_COMPLETION_RESERVE,
    ENV_API_KEY,
    GroqHostedProvider,
    RequestMode,
    policy_from_env,
    preflight,
)
from surge.analysis.tokenizer import exact_tokens_or_none

PROBE_VERSION = "groq-probe-1.1.0"

#: A compound system's rate-limit headers describe the system, not the models
#: it calls. Measured 2026-09-18 (D-256): its usage breakdown lists
#: openai/gpt-oss-120b and meta-llama/llama-4-scout each receiving the whole
#: prompt, and those models' own limits appear in no header - so for compound,
#: "fits the measured limits" is not evidence of acceptance.
COMPOUND_FIT_CAVEAT = (
    ". These are the compound system's own limits; the models it calls internally keep "
    "theirs, which no header shows (its usage breakdown lists openai/gpt-oss-120b and "
    "meta-llama/llama-4-scout each receiving the whole prompt), so only the contract smoke "
    "can say whether the request is accepted"
)


def describe_failure(exc: BaseException) -> str:
    """One line saying what went wrong, including Groq's own error message.

    An HTTP 400 on its own says only that the request was refused. Groq's error
    body says why - which parameter, which model - and it is the provider's
    description of the request, never the request's headers, so it cannot carry
    the key. Only the documented fields of the error object are kept.
    """

    import urllib.error

    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        except (ValueError, OSError):
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            parts = [
                f"{field}={error[field]}"
                for field in ("type", "code", "param", "message")
                if error.get(field)
            ]
            if parts:
                detail += " | groq: " + "; ".join(parts)
    # Groq names the organisation in rate-limit messages; keep it out of records.
    import re

    return re.sub(r"org_[A-Za-z0-9]+", "org_<redacted>", detail)


def default_state_dir() -> Path:
    configured = os.environ.get("SURGE_STATE_DIR")
    return Path(configured) if configured else Path.home() / ".surge"


def record_path(state_dir: Path, model: str) -> Path:
    return Path(state_dir) / "groq_probe" / (model.replace("/", "__") + ".json")


def read_record(state_dir: Path, model: str) -> dict | None:
    path = record_path(state_dir, model)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_prompt() -> str:
    # The same floor the feasibility report measures: the real canonical text and
    # the real addenda, with an empty bundle. Anything a production request adds
    # on top only makes it larger.
    from surge.jobs.analysis_feasibility import (
        CANONICAL_PATH,
        _addenda_texts,
        _minimum_entry_prompt,
    )

    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    return _minimum_entry_prompt(canonical, _addenda_texts())


def probe_model(model: str, *, env=None) -> dict:
    source = dict(env if env is not None else os.environ)
    source["GROQ_MODEL"] = model
    provider = GroqHostedProvider.from_env(source, mode=RequestMode.RESEARCH_SMOKE)

    measured = provider.quota_probe()

    prompt = _entry_prompt()
    check = preflight(prompt, quota=measured, reserved_output_tokens=DEFAULT_COMPLETION_RESERVE)
    exact = exact_tokens_or_none(prompt, encoding="o200k_harmony")

    policy = policy_from_env(source)
    reason = check.reason
    if check.fits and model.startswith(COMPOUND_MODEL_PREFIXES):
        reason += COMPOUND_FIT_CAVEAT
    return {
        "probe_version": PROBE_VERSION,
        "model": model,
        "measured_at": datetime.now(UTC).isoformat(),
        "live_request_succeeded": True,
        "limits": asdict(measured),
        "output_mode": provider.output_mode.value,
        "entry_request_prompt_tokens_estimated": check.prompt_tokens_estimated,
        "entry_request_prompt_tokens_exact_o200k_harmony": exact,
        "reserved_output_tokens": check.reserved_output_tokens,
        "entry_request_fits": check.fits,
        "entry_request_reason": reason,
        "entry_request_reason_code": check.reason_code,
        "privacy_gate_passes": policy.privacy_gate_passes,
        "privacy_gate_detail": policy.privacy_gate_detail,
    }


#: The only security the contract smoke ever names. Deliberately not a real
#: code, so no answer to it can be mistaken for an opinion about a stock.
SMOKE_SECURITY = "SYNTHETIC-CONTRACT-SMOKE"


def contract_smoke(model: str, *, env=None) -> dict:
    """One real Entry request, end to end, against a synthetic bundle.

    The quota probe proves the account answers; it says nothing about whether
    the model's output can carry the Entry contract. This does: the real
    canonical method and addenda, rendered exactly as production renders them,
    through the production privacy gate, parsed and schema-checked with the
    bounded retry the JSON_OBJECT path relies on.

    The bundle is synthetic and says so, and the answer is used for nothing: it
    is not stored as a prediction, not written to the database, and not a
    teacher input. What is recorded is only whether the contract held.
    """

    from datetime import date

    from surge.analysis.bundle import sha256_text
    from surge.analysis.entry_analysis import IntradayBundle, render_entry_prompt
    from surge.analysis.llm import LLMRequest
    from surge.jobs.analysis_feasibility import CANONICAL_PATH, _addenda_texts

    source = dict(env if env is not None else os.environ)
    source["GROQ_MODEL"] = model
    # PRODUCTION mode, deliberately: this sends the canonical method, so it has
    # to pass the gate a production request passes - ZDR confirmed on.
    provider = GroqHostedProvider.from_env(source)
    provider.quota_probe()

    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    addenda = _addenda_texts()
    now = datetime.now(UTC)
    bundle = IntradayBundle(
        security_id=SMOKE_SECURITY,
        market_code="JP",
        session_date=date(now.year, now.month, now.day),
        decision_cutoff_at=now,
        canonical_prompt_sha256=sha256_text(canonical),
        addenda_sha256=[sha256_text(text) for text in addenda],
        sections={
            "security": {
                "note": "SYNTHETIC CONTRACT SMOKE TEST. Not a real security; no real data.",
            },
            "live_price": {"price": "1000", "currency": "JPY", "synthetic": True},
            "stage3_setup": {"state": "WATCH_BREAKOUT", "synthetic": True},
            "coverage": {"materials": 1.0, "synthetic": True},
        },
    )
    prompt = render_entry_prompt(bundle, canonical, addenda)
    started = datetime.now(UTC)
    response = provider.analyse_entry(LLMRequest(prompt=prompt, bundle=bundle))
    return {
        "contract_smoke_passed": True,
        "contract_smoke_at": started.isoformat(),
        "contract_smoke_returned_state": response.state.value,
        "contract_smoke_output_mode": provider.output_mode.value,
        "contract_smoke_security": SMOKE_SECURITY,
        "contract_smoke_note": (
            "the answer was schema-checked and discarded; it is not a prediction and was not "
            "stored anywhere else"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        required=True,
        help="a Groq model id; repeat to probe several",
    )
    parser.add_argument("--state-dir", default=str(default_state_dir()))
    parser.add_argument(
        "--contract-smoke",
        action="store_true",
        help=(
            "also send one real Entry request with a synthetic bundle, through the production "
            "privacy gate, to check the output contract holds end to end"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not (os.environ.get(ENV_API_KEY) or "").strip():
        print(
            f"{ENV_API_KEY} is not in this process's environment. Run through "
            "ops\\windows\\Invoke-WithSurgeSecrets.ps1 after storing it with Set-SurgeSecret.ps1",
            file=sys.stderr,
        )
        return 2

    results = []
    failed = False
    for model in args.model:
        try:
            result = probe_model(model)
        # Anything at all, reported as one line. An HTTP error's text is its
        # status and URL; neither the request headers nor the key are part of
        # it, and a traceback is not printed.
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            failed = True
            result = {
                "model": model,
                "measured_at": datetime.now(UTC).isoformat(),
                "live_request_succeeded": False,
                "error": describe_failure(exc),
            }
        if args.contract_smoke and result.get("live_request_succeeded"):
            if not result.get("entry_request_fits"):
                result["contract_smoke_passed"] = False
                result["contract_smoke_skipped"] = (
                    "the Entry request does not fit this model's measured limits, so it was not sent"
                )
            else:
                try:
                    result.update(contract_smoke(model))
                except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                    failed = True
                    result["contract_smoke_passed"] = False
                    result["contract_smoke_error"] = describe_failure(exc)
        path = record_path(Path(args.state_dir), model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        results.append(result)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"== {r['model']}")
            if not r.get("live_request_succeeded"):
                print(f"   probe failed: {r.get('error')}")
                continue
            limits = r["limits"]
            print(
                f"   limits      TPM {limits.get('max_tokens_per_minute')}  "
                f"RPM {limits.get('max_requests_per_minute')}  RPD {limits.get('requests_per_day')}"
            )
            print(f"   output mode {r['output_mode']}")
            tokens = (
                r["entry_request_prompt_tokens_exact_o200k_harmony"]
                or r["entry_request_prompt_tokens_estimated"]
            )
            verdict = "FITS" if r["entry_request_fits"] else "DOES NOT FIT"
            print(
                f"   entry request {tokens} prompt tokens + {r['reserved_output_tokens']} "
                f"reserved -> {verdict}"
            )
            if not r["entry_request_fits"]:
                print(f"   {r['entry_request_reason_code']}: {r['entry_request_reason']}")
            gate = "PASS" if r["privacy_gate_passes"] else "BLOCKED"
            print(f"   privacy gate {gate}: {r['privacy_gate_detail']}")
            if "contract_smoke_passed" in r:
                if r["contract_smoke_passed"]:
                    print(
                        f"   contract smoke PASS: a schema-valid "
                        f"{r['contract_smoke_returned_state']} came back and was discarded"
                    )
                elif r.get("contract_smoke_skipped"):
                    print(f"   contract smoke skipped: {r['contract_smoke_skipped']}")
                else:
                    print(f"   contract smoke FAILED: {r.get('contract_smoke_error')}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
