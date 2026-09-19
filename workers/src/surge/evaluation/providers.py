"""The two ways a request reaches Jev (D-275): TypeSafe's own API, and Vercel AI Gateway as fallback.

**TypeSafe direct** (primary). ``POST https://api.typesafe.ai/v1/systemone``
with ``model: "jev-latest"``, key ``TYPESAFE_API_KEY``. The answer names the
served version (``jev-1.13.0`` on 2026-09-19). TypeSafe reports no cost, so the
cost is its published price times the input tokens (output is free). A 429 may
carry the wait the server wants (``retry-after-ms``, ``retry-after`` or a body
``retry_after_ms``); it is parsed and recorded, and the run honours it.

**Vercel AI Gateway** (fallback: an outage of the direct API, output
comparisons, adapter regression tests). ``typesafe-ai/jev`` through the Node
runner, as Phase A ran it; its free tier refuses the sixth request in a short
window (D-273, D-274), so it is not the Phase B route.

Both read the same stored evaluation request - the Gateway's wire form, which
``build`` writes and preflight checks - and the direct adapter derives its own
wire form from it: the same state and the same question text, with the model
alias and the yes/no type named as TypeSafe's API names them (``noul`` where
the Gateway says ``boolean``). Each record keeps the hash of the bytes actually
sent. Neither adapter writes the request, the canonical text or the key
anywhere; the raw response is saved as it came, before any conversion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path

from surge.analysis.jev_questions import (
    ENDPOINT,
    GATEWAY_MODEL,
    MODEL,
    USD_PER_MILLION_INPUT_TOKENS,
    completeness,
    questions,
    to_gateway,
)

TYPESAFE_DIRECT = "typesafe-direct"
VERCEL_GATEWAY = "vercel-ai-gateway"
PROVIDER_NAMES = (TYPESAFE_DIRECT, VERCEL_GATEWAY)
ENV_TYPESAFE_KEY = "TYPESAFE_API_KEY"
#: Who answers either way: the Gateway's routing names it, the direct API is it.
SERVED_PROVIDER = "typesafe-ai"
SERVED_VERSION = re.compile(r"^jev-\d+\.\d+\.\d+$")
_PRICE = Decimal(str(USD_PER_MILLION_INPUT_TOKENS))


class ProviderError(RuntimeError):
    pass


def retry_after_seconds(headers: dict, body: dict | None) -> float | None:
    """The server's requested wait: ``retry-after-ms``, then ``retry-after`` (seconds or date), then the body."""

    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        value = lower.get(name)
        if value in (None, ""):
            continue
        try:
            return max(0.0, float(value) / scale)
        except ValueError:
            if name == "retry-after":
                try:
                    return max(0.0, (parsedate_to_datetime(str(value)) - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError):
                    pass
    for holder in (body or {}, (body or {}).get("error") if isinstance((body or {}).get("error"), dict) else {}):
        value = holder.get("retry_after_ms") if isinstance(holder, dict) else None
        if isinstance(value, int | float):
            return max(0.0, value / 1000.0)
    return None


def _error_summary(status: int | None, parsed) -> dict:
    """What a failure says, short: never the whole body, which could echo the request."""

    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return {"status": status, "type": error.get("type"), "message": str(error.get("message") or "")[:120]}
    if isinstance(error, str):
        return {"status": status, "type": None, "message": error[:120]}
    return {"status": status, "type": None, "message": None}


def direct_wire_body(stored: dict) -> bytes:
    """TypeSafe's wire form of a stored (Gateway-form) evaluation request: same state, same questions."""

    asked = questions()
    if stored.get("questions") != to_gateway(asked):
        raise ProviderError("the stored request's questions are not the evaluation's questions")
    return json.dumps({"model": MODEL, "state": stored["state"], "questions": asked}, ensure_ascii=False).encode("utf-8")


def urllib_transport(url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - a fixed https endpoint
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        raise ProviderError(f"{path} exists; a raw response is written once")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@dataclass
class TypeSafeDirect:
    transport: Callable[[str, bytes, dict, float], tuple[int, dict, bytes]] = urllib_transport
    timeout: float = 120.0
    name: str = TYPESAFE_DIRECT
    model_requested: str = MODEL
    #: TypeSafe says what to wait on a 429; the run honours it (D-275).
    honours_rate_limit_wait: bool = True

    def call(self, request_path: Path, raw_out: Path, *, label: str, request_sha256: str) -> dict:
        wire = direct_wire_body(json.loads(request_path.read_bytes()))
        key = (os.environ.get(ENV_TYPESAFE_KEY) or "").strip()
        if not key:
            raise ProviderError(f"{ENV_TYPESAFE_KEY} is not in this process's environment; "
                                "run through Invoke-WithSurgeSecrets.ps1")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json; charset=utf-8",
                   "Accept": "application/json", "User-Agent": "surge-jev-eval"}
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        try:
            status, response_headers, raw = self.transport(ENDPOINT, wire, headers, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status, response_headers, raw = None, {}, json.dumps({"error": {"type": type(exc).__name__}}).encode()
        latency = time.perf_counter() - started
        _write_once(raw_out, raw)  # the raw Jev output as it came
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = {}
        ok = status == 200 and isinstance(parsed.get("answers"), dict)
        usage = parsed.get("usage") or {}
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        cost = Decimal(tokens_in) * _PRICE / Decimal(1_000_000) if isinstance(tokens_in, int) else None
        lower = {k.lower(): v for k, v in response_headers.items()}
        return {
            "label": label,
            "via": self.name,
            "sent_at": started_at,
            "request_sha256": request_sha256,
            "wire_sha256": hashlib.sha256(wire).hexdigest(),
            "status": "ok" if ok else "error",
            "error": None if ok else _error_summary(status, parsed),
            "latency_seconds": round(latency, 3),
            "model": parsed.get("model"),
            "resolved_provider": SERVED_PROVIDER if status is not None else None,
            "request_id": lower.get("x-typesafe-request-id"),
            "usage": ({"inputTokens": tokens_in, "outputTokens": tokens_out,
                       "totalTokens": (tokens_in or 0) + (tokens_out or 0)} if ok else None),
            "cost_usd": None if cost is None else str(cost),
            "cost_basis": "TypeSafe published price x input tokens (output free); TypeSafe reports no cost",
            "gateway_cost_usd": None,
            "answers": parsed.get("answers") if ok else None,
            # The direct API puts confidence on choice and score answers itself.
            "completeness_problems": completeness(questions(), parsed) if ok else ["no answers"],
            "http": {
                "status": status,
                "error_name": None if ok else ("TypeSafeRateLimitError" if status == 429 else "TypeSafeAPIError"),
                "error_type": None if ok else _error_summary(status, parsed)["type"],
                "retry_after": lower.get("retry-after"),
                "retry_after_ms": lower.get("retry-after-ms"),
                "retry_after_seconds": None if ok else retry_after_seconds(response_headers, parsed),
                "response_headers": response_headers,
            },
        }


def gateway_http_facts(result: dict) -> dict:
    """The HTTP side of one Gateway call as the runner saw it: status, error type, Retry-After, headers."""

    error = result.get("error")
    if not error:
        return {"status": 200, "error_name": None, "error_type": None, "retry_after": None,
                "retry_after_seconds": None, "response_headers": (result.get("response") or {}).get("headers")}
    headers = error.get("responseHeaders") or {}
    retry_after = next((str(v) for k, v in headers.items() if k.lower() == "retry-after"), None)
    return {"status": error.get("statusCode"), "error_name": error.get("name"), "error_type": error.get("type"),
            "retry_after": retry_after, "retry_after_seconds": retry_after_seconds(headers, None),
            "response_headers": headers or None}


@dataclass
class VercelGateway:
    runner: Path
    name: str = VERCEL_GATEWAY
    model_requested: str = GATEWAY_MODEL
    #: The free tier sends no wait and refuses again soon (D-273): a 429 stops the run.
    honours_rate_limit_wait: bool = False

    def call(self, request_path: Path, raw_out: Path, *, label: str, request_sha256: str) -> dict:
        from surge.jobs.jev_smoke import gateway_record

        raw_out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["node", str(self.runner), str(request_path), str(raw_out)], check=False,
                       timeout=300, cwd=str(self.runner.parent), capture_output=True, text=True)
        result = (json.loads(raw_out.read_text(encoding="utf-8")) if raw_out.exists()
                  else {"error": {"message": "the runner wrote nothing"}, "latencyMs": 0})
        record = gateway_record(result, asked=questions(), label=label, request_sha256=request_sha256)
        record["wire_sha256"] = request_sha256  # the runner sends the stored bytes as they are
        record["cost_usd"] = record.get("gateway_cost_usd")
        record["cost_basis"] = "reported by the Gateway"
        record["runner_started_at"] = result.get("startedAt")
        record["http"] = gateway_http_facts(result)
        return record


def provider_for(name: str, *, runner: Path, transport=None):
    if name == TYPESAFE_DIRECT:
        return TypeSafeDirect(**({"transport": transport} if transport else {}))
    if name == VERCEL_GATEWAY:
        return VercelGateway(runner=runner)
    raise ProviderError(f"unknown provider {name!r}; one of {PROVIDER_NAMES}")


def record_cost(record: dict) -> Decimal | None:
    """The cost a record carries, whichever provider wrote it (older Gateway records: gateway_cost_usd only)."""

    cost = record.get("cost_usd", record.get("gateway_cost_usd"))
    return None if cost is None else Decimal(str(cost))


__all__ = ["ENV_TYPESAFE_KEY", "PROVIDER_NAMES", "SERVED_PROVIDER", "SERVED_VERSION", "TYPESAFE_DIRECT",
           "VERCEL_GATEWAY", "ProviderError", "TypeSafeDirect", "VercelGateway", "direct_wire_body",
           "gateway_http_facts", "provider_for", "record_cost", "retry_after_seconds", "urllib_transport"]
