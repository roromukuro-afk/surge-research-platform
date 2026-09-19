"""The run's hard budget - dollars and requests - checked before every request.

Jev through the Gateway is charged on input tokens only: $0.042 per million,
output free (D-269; measured 2026-09-18 at $0.000918036 for 21,858 input
tokens). Three limits apply, and a request that would break any of them is not
sent:

* the run's hard budget in **requests**;
* the run's hard budget in **dollars**: what the write-once responses say was
  spent, plus a conservative estimate of the next request;
* the **credit balance** read before the run starts: the budget may not
  exceed it, nor the $5 free credit. No paid credit is bought and auto-reload
  stays off (D-270). The Gateway's is read through the runner; TypeSafe has
  no balance API, so its balance is the last console snapshot the operator
  recorded - the confirmed balance, when, and the expiry the console showed -
  less what this system has recorded spending since (D-272). The snapshot
  holds until that expiry; past it nothing is sent until a new balance is
  confirmed.

The estimate is the request's o200k token count times a safety factor. Jev's
tokenizer counted 1.23x the o200k count of the smoke's state (21,858 against
17,750, questions included on Jev's side only), so 1.35x leaves room. A request
whose cost the Gateway did not report - an error, say - is charged at its
estimate, never at zero.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from surge.analysis.jev_questions import USD_PER_MILLION_INPUT_TOKENS

TOKEN_FACTOR = Decimal("1.35")
FREE_CREDIT_USD = Decimal("5")
#: Jev's context window (TypeSafe). A request estimated past it is not sent.
CONTEXT_TOKENS = 32_000
_PRICE = Decimal(str(USD_PER_MILLION_INPUT_TOKENS))


class BudgetExceeded(RuntimeError):
    pass


def estimated_jev_tokens(o200k_tokens: int) -> int:
    return int((Decimal(o200k_tokens) * TOKEN_FACTOR).to_integral_value(rounding=ROUND_CEILING))


def estimate_usd(o200k_tokens: int) -> Decimal:
    """A conservative price for one request of this many o200k tokens."""

    return estimated_jev_tokens(o200k_tokens) * _PRICE / Decimal(1_000_000)


@dataclass(frozen=True)
class Budget:
    max_usd: Decimal
    max_requests: int

    def __post_init__(self) -> None:
        if self.max_usd <= 0 or self.max_requests <= 0:
            raise ValueError("a budget needs a positive amount and a positive request count")
        if self.max_usd > FREE_CREDIT_USD:
            raise ValueError(f"a run budget above the ${FREE_CREDIT_USD} free credit is not allowed")


@dataclass
class Ledger:
    """What has been spent, from the responses on disk; nothing else counts."""

    budget: Budget
    spent_usd: Decimal = Decimal(0)
    requests: int = 0
    estimated_charges: list[str] = field(default_factory=list)

    def record(self, response: dict, *, estimate: Decimal) -> None:
        self.requests += 1
        cost = response.get("cost_usd", response.get("gateway_cost_usd"))
        if cost is None:
            self.spent_usd += estimate
            self.estimated_charges.append(str(response.get("label")))
        else:
            self.spent_usd += Decimal(str(cost))

    def check_next(self, estimate: Decimal) -> None:
        if self.requests + 1 > self.budget.max_requests:
            raise BudgetExceeded(f"request {self.requests + 1} would pass the run's {self.budget.max_requests} requests")
        if self.spent_usd + estimate > self.budget.max_usd:
            raise BudgetExceeded(
                f"${self.spent_usd} spent + ${estimate:.6f} estimated would pass the run's ${self.budget.max_usd}"
            )


def plan_problems(budget: Budget, estimates: list[Decimal], credits: dict | None) -> list[str]:
    """Why the planned requests may not be sent under ``budget``; empty when they may."""

    problems = []
    if len(estimates) > budget.max_requests:
        problems.append(f"{len(estimates)} requests planned, the budget allows {budget.max_requests}")
    total = sum(estimates, Decimal(0))
    if total > budget.max_usd:
        problems.append(f"the planned requests are estimated at ${total:.4f}, above the budget ${budget.max_usd}")
    if credits is None:
        problems.append("the credit balance has not been read")
    elif "error" in credits:
        problems.append(f"the credit balance could not be used: {credits['error']}")
    elif budget.max_usd > Decimal(str(credits["balance"])):
        problems.append(f"the budget ${budget.max_usd} is above the credit balance ${credits['balance']}")
    return problems


CREDIT_DIR = "credit"
#: One-off direct checks outside any run, with the cost at the published price.
_ONE_OFF_DIRECT = ("direct-compare-*/response.direct.json", "direct-throughput-smoke-*/response-*.json",
                   "direct-check-*/response.json")


def charged_usd(record: dict, estimate: Decimal | None = None) -> Decimal:
    """What a response record counts against a budget: the ledger's figure, the cost, or the estimate."""

    for key in ("ledger_usd", "cost_usd", "gateway_cost_usd"):
        if record.get(key) is not None:
            return Decimal(str(record[key]))
    if estimate is None:
        raise ValueError(f"{record.get('label')!r} carries no cost and no estimate was given for it")
    return estimate


def local_direct_spend(root: Path, since: datetime) -> Decimal:
    """What this system spent on TypeSafe's own API after ``since``, from the records it wrote.

    Every response record under the evaluation root (Phase A and Phase B runs
    alike, at any depth), and the one-off checks. A record's ``ledger_usd`` -
    its cost, or its estimate when no cost was known - is what counts.
    """

    total = Decimal(0)
    base = root / "evaluation" / "jev"
    for path in base.glob("**/responses/*.json"):
        if ".raw" in path.name:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("via") != "typesafe-direct" or datetime.fromisoformat(record["sent_at"]) <= since:
            continue
        for key in ("ledger_usd", "cost_usd"):
            if record.get(key) is not None:
                total += Decimal(str(record[key]))
                break
    for pattern in _ONE_OFF_DIRECT:
        for path in base.glob(pattern):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("cost_usd_at_published_price") and datetime.fromisoformat(record["sent_at"]) > since:
                total += Decimal(str(record["cost_usd_at_published_price"]))
    return total


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} needs a timezone")
    return value


def record_credit_snapshot(root: Path, *, balance_usd: Decimal, confirmed_at: datetime, expires_at: datetime,
                           displayed_expiry: str, source: str, now: datetime | None = None) -> dict:
    """Keep one reading of TypeSafe's console (write-once, numbered): the balance, when, and the expiry shown."""

    now = now or datetime.now(UTC)
    _aware(confirmed_at, "confirmed_at")
    _aware(expires_at, "expires_at")
    if balance_usd < 0 or balance_usd > FREE_CREDIT_USD:
        raise ValueError(f"a balance of ${balance_usd} is outside the ${FREE_CREDIT_USD} monthly credit")
    if confirmed_at > now:
        raise ValueError("a balance cannot be confirmed in the future")
    if expires_at <= confirmed_at:
        raise ValueError("the credit expires before it was confirmed")
    directory = root / "evaluation" / "jev" / CREDIT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    n = 1
    while (directory / f"typesafe-{n}.json").exists():
        n += 1
    snapshot = {
        "provider": "typesafe-direct",
        "confirmed_balance_usd": str(balance_usd),
        "confirmed_at": confirmed_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "displayed_expiry": displayed_expiry,
        "source": source,
        "recorded_at": now.isoformat(),
        "file": f"typesafe-{n}.json",
    }
    with (directory / f"typesafe-{n}.json").open("x", encoding="utf-8") as handle:  # write-once
        handle.write(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    return snapshot


def latest_credit_snapshot(root: Path) -> dict | None:
    directory = root / "evaluation" / "jev" / CREDIT_DIR
    if not directory.exists():
        return None
    numbered = sorted(directory.glob("typesafe-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    return json.loads(numbered[-1].read_text(encoding="utf-8")) if numbered else None


def direct_credit_state(root: Path, *, now: datetime | None = None) -> dict:
    """TypeSafe's credit before a run: the latest console snapshot less the local spend since it.

    Valid until the expiry the console showed - there is no other age limit.
    Past it nothing is sent until the new balance has been confirmed once.
    """

    snapshot = latest_credit_snapshot(root)
    if snapshot is None:
        return {"error": "no TypeSafe credit snapshot: read Credit Balance and the credit's expiry at "
                         "console.typesafe.ai/settings/billing and record them (jev_eval record-credit)"}
    now = now or datetime.now(UTC)
    confirmed_at = datetime.fromisoformat(snapshot["confirmed_at"])
    expires_at = datetime.fromisoformat(snapshot["expires_at"])
    if confirmed_at > now:
        return {"error": f"the snapshot {snapshot['file']} is confirmed in the future ({snapshot['confirmed_at']})"}
    if now >= expires_at:
        return {"error": f"the credit confirmed at {snapshot['confirmed_at']} expired at {snapshot['expires_at']} "
                         f"(shown as {snapshot['displayed_expiry']!r}): confirm the new console balance once and "
                         "record it (jev_eval record-credit)", "expired": True}
    spent = local_direct_spend(root, confirmed_at)
    return {"balance": str(Decimal(snapshot["confirmed_balance_usd"]) - spent),
            "confirmed_balance": snapshot["confirmed_balance_usd"], "confirmed_at": snapshot["confirmed_at"],
            "expires_at": snapshot["expires_at"], "displayed_expiry": snapshot["displayed_expiry"],
            "spent_since_confirmation": str(spent), "snapshot": snapshot["file"],
            "source": "the operator's console snapshot less this system's recorded spend since"}


def read_credits(runner: Path, out: Path) -> dict:
    """The Gateway's balance and total used, through the runner. Not a model request.

    The runner needs AI_GATEWAY_API_KEY in its environment (run through
    Invoke-WithSurgeSecrets.ps1); the key never passes through here.
    """

    if out.exists():
        raise FileExistsError(f"{out} exists; a balance is read into a new file so an old one is never taken for it")
    completed = subprocess.run(["node", str(runner), "--credits", str(out)], check=False, timeout=60,
                               cwd=str(runner.parent), capture_output=True, text=True)
    if not out.exists():
        return {"error": f"the runner wrote nothing (exit {completed.returncode})"}
    result = json.loads(out.read_text(encoding="utf-8"))
    if "error" in result:
        return {"error": (result["error"] or {}).get("message")}
    return {"balance": result["balance"], "total_used": result["totalUsed"], "checked_at": result["checkedAt"]}


__all__ = ["CONTEXT_TOKENS", "CREDIT_DIR", "FREE_CREDIT_USD", "TOKEN_FACTOR", "Budget", "BudgetExceeded", "Ledger",
           "charged_usd", "direct_credit_state", "estimate_usd", "estimated_jev_tokens", "latest_credit_snapshot",
           "local_direct_spend", "plan_problems", "read_credits", "record_credit_snapshot"]
