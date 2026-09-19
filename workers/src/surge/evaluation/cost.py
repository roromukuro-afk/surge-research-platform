"""The run's hard budget - dollars and requests - checked before every request.

Jev through the Gateway is charged on input tokens only: $0.042 per million,
output free (D-269; measured 2026-09-18 at $0.000918036 for 21,858 input
tokens). Three limits apply, and a request that would break any of them is not
sent:

* the run's hard budget in **requests**;
* the run's hard budget in **dollars**: what the write-once responses say was
  spent, plus a conservative estimate of the next request;
* the **Gateway credit balance** read before the run starts: the budget may not
  exceed it, nor the $5 free credit. No paid credit is bought and auto-reload
  stays off (D-270).

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
from datetime import UTC, datetime, timedelta
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
        problems.append("the Gateway credit balance has not been read")
    elif "error" in credits:
        problems.append(f"the Gateway credit balance could not be read: {credits['error']}")
    elif budget.max_usd > Decimal(str(credits["balance"])):
        problems.append(f"the budget ${budget.max_usd} is above the credit balance ${credits['balance']}")
    return problems


#: How old a balance read from TypeSafe's console may be (D-275). TypeSafe has
#: no balance API, so the operator reads it; what this system spent since is
#: subtracted from its own records.
DIRECT_BALANCE_MAX_AGE = timedelta(days=7)


def local_direct_spend(root: Path, since: datetime) -> Decimal:
    """What this system spent on TypeSafe's own API after ``since``, from the records it wrote."""

    total = Decimal(0)
    base = root / "evaluation" / "jev"
    for path in base.glob("*/responses/*.json"):
        if path.name.endswith(".raw.json") or ".raw." in path.name:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("via") == "typesafe-direct" and record.get("cost_usd") is not None and \
                datetime.fromisoformat(record["sent_at"]) > since:
            total += Decimal(str(record["cost_usd"]))
    # One-off checks outside any run (the 2026-09-19 comparison and rate-limit smoke).
    for pattern in ("direct-compare-*/response.direct.json", "direct-throughput-smoke-*/response-*.json"):
        for path in base.glob(pattern):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("cost_usd_at_published_price") and datetime.fromisoformat(record["sent_at"]) > since:
                total += Decimal(str(record["cost_usd_at_published_price"]))
    return total


def direct_credit_state(balance_usd: Decimal | None, checked_at: datetime | None, *, root: Path,
                        now: datetime | None = None) -> dict:
    """TypeSafe's monthly credit before a run: the operator's reading, less local spend since it."""

    if balance_usd is None or checked_at is None:
        return {"error": "TypeSafe has no balance API: read Credit Balance at console.typesafe.ai/settings/billing "
                         "and pass --credit-balance-usd and --credit-checked-at"}
    now = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        return {"error": "--credit-checked-at needs a timezone"}
    if now - checked_at > DIRECT_BALANCE_MAX_AGE or checked_at > now:
        return {"error": f"the balance read at {checked_at.isoformat()} is not within the last "
                         f"{DIRECT_BALANCE_MAX_AGE.days} days"}
    spent = local_direct_spend(root, checked_at)
    return {"balance": str(balance_usd - spent), "attested_balance": str(balance_usd),
            "checked_at": checked_at.isoformat(), "spent_since_check": str(spent),
            "source": "operator reading of the TypeSafe console, less this system's recorded spend since"}


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


__all__ = ["CONTEXT_TOKENS", "DIRECT_BALANCE_MAX_AGE", "FREE_CREDIT_USD", "TOKEN_FACTOR", "Budget", "BudgetExceeded",
           "Ledger", "direct_credit_state", "estimate_usd", "estimated_jev_tokens", "local_direct_spend",
           "plan_problems", "read_credits"]
