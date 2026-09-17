"""`python -m surge.jobs.analysis_feasibility` - can a free tier run this analysis.

Answerable today, with no credential and no account, because the size of the
input is a property of this repository rather than of any provider. Canonical
v5.1 is registered and hashed; the addenda are files; the bundle's fixed
scaffolding is code. What a request costs is therefore measurable now, and
comparing it against a provider's *published* limits decides whether an account
is worth opening at all.

Two honesty constraints shape the output.

**The estimate is an estimate.** Nobody here is running the provider's
tokeniser. The count treats a CJK character as roughly one token and Latin text
as roughly one per three and a half characters, which is deliberately the
pessimistic direction for a Japanese prompt: being told a request fits and then
having it rejected is worse than the reverse.

**Published limits are not the account's limits.** They decide whether to
bother; they never decide whether to send. ``quota_probe()`` measures the real
ones before anything carrying the method is transmitted.

The one thing this must never conclude is that the canonical prompt should be
shortened. If v5.1 does not fit, the finding is about the tier.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from surge.analysis.bundle import InputBundle
from surge.analysis.entry_analysis import IntradayBundle, render_entry_prompt
from surge.analysis.groq_provider import (
    ANALYSIS_FREE_QUOTA_BLOCKED,
    PUBLISHED_FREE_LIMITS,
    estimate_tokens,
)
from surge.analysis.llm import render_prompt

REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_PATH = REPO_ROOT / "docs" / "prompts" / "short-surge-v5.1.original.md"
ADDENDA_DIR = REPO_ROOT / "docs" / "prompts" / "addenda"

CUTOFF = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
DIGEST = "0" * 64


@dataclass(frozen=True)
class Measurement:
    canonical_only: int
    canonical_plus_addenda: int
    minimum_stage3_request: int
    minimum_entry_request: int
    canonical_bytes: int
    addenda_files: int

    @property
    def summary(self) -> dict:
        return {
            "canonical_only_estimated_tokens": self.canonical_only,
            "canonical_plus_addenda_estimated_tokens": self.canonical_plus_addenda,
            "minimum_stage3_request_tokens": self.minimum_stage3_request,
            "minimum_entry_request_tokens": self.minimum_entry_request,
            "canonical_bytes": self.canonical_bytes,
            "addenda_files": self.addenda_files,
        }


def _addenda_texts() -> list[str]:
    if not ADDENDA_DIR.exists():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(ADDENDA_DIR.glob("*.md"))]


def _minimum_stage3_prompt(canonical: str, addenda: list[str]) -> str:
    """The smallest honest Stage 3 request: the contract, and no data.

    Every section is empty. A real request is strictly larger than this, so the
    number is a floor rather than a guess - which is what makes "it does not
    fit" a safe conclusion and "it fits" only a provisional one.
    """

    bundle = InputBundle(
        security_id="FLOOR",
        market_code="JP",
        as_of_date=date(2026, 9, 17),
        knowledge_cutoff=CUTOFF,
        canonical_prompt_sha256=DIGEST,
        sections={},
    )
    return render_prompt(bundle, canonical, addenda)


def _minimum_entry_prompt(canonical: str, addenda: list[str]) -> str:
    bundle = IntradayBundle(
        security_id="FLOOR",
        market_code="JP",
        session_date=date(2026, 9, 17),
        decision_cutoff_at=CUTOFF,
        canonical_prompt_sha256=DIGEST,
        sections={},
    )
    return render_entry_prompt(bundle, canonical, addenda)


def measure() -> Measurement:
    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    addenda = _addenda_texts()
    return Measurement(
        canonical_only=estimate_tokens(canonical),
        canonical_plus_addenda=estimate_tokens(canonical + "\n".join(addenda)),
        minimum_stage3_request=estimate_tokens(_minimum_stage3_prompt(canonical, addenda)),
        minimum_entry_request=estimate_tokens(_minimum_entry_prompt(canonical, addenda)),
        canonical_bytes=len(canonical.encode("utf-8")),
        addenda_files=len(addenda),
    )


def assess_tier(measurement: Measurement, name: str, limits: dict) -> dict:
    """Compare the floor against one published tier, and name the consequence."""

    tpm = limits.get("tokens_per_minute")
    tpd = limits.get("tokens_per_day")
    floor = max(measurement.minimum_stage3_request, measurement.minimum_entry_request)

    verdict: dict = {
        "model_family": name,
        "limits": dict(limits),
        "largest_minimum_request": floor,
    }

    if tpm is not None and floor > tpm:
        verdict["fits"] = False
        verdict["reason_code"] = ANALYSIS_FREE_QUOTA_BLOCKED
        verdict["why"] = (
            f"the smallest possible request is about {floor:,} tokens and this family allows "
            f"{tpm:,} tokens per minute. One request cannot be sent at all, whatever the pacing"
        )
        return verdict

    verdict["fits"] = True
    if tpm:
        verdict["requests_per_minute_at_this_size"] = max(1, tpm // floor)
    if tpd:
        verdict["requests_per_day_at_this_size"] = tpd // floor
    if limits.get("requests_per_day") is not None:
        verdict["requests_per_day_cap"] = limits["requests_per_day"]
    verdict["why"] = "the floor fits this family's published per-minute limit"
    return verdict


def assess(measurement: Measurement, tiers: dict | None = None) -> dict:
    """Every published tier, because one tier's limit is not the provider's.

    Reporting a single verdict for "the free tier" would repeat the error that
    made the first Alpaca conclusion wrong: a constraint that holds for one
    group generalised to everything the vendor offers. The families here differ
    by nearly an order of magnitude.
    """

    tiers = tiers or PUBLISHED_FREE_LIMITS
    per_tier = [assess_tier(measurement, name, limits) for name, limits in tiers.items()]
    usable = [
        tier
        for tier in per_tier
        if tier["fits"] and tier["limits"].get("strict_structured_outputs") is True
    ]
    possible = [tier for tier in per_tier if tier["fits"]]

    if usable:
        conclusion = (
            "a free tier can carry this analysis with strict structured outputs: "
            + ", ".join(t["model_family"] for t in usable)
        )
        reason_code = None
    elif possible:
        conclusion = (
            "no free family both fits and is documented for strict structured outputs. "
            + ", ".join(t["model_family"] for t in possible)
            + " fits on size; whether it supports the structured output contract is unverified "
            "and has to be measured before it can be relied on"
        )
        reason_code = None
    else:
        conclusion = "no published free family can carry one request of this size"
        reason_code = ANALYSIS_FREE_QUOTA_BLOCKED

    return {
        "tiers": per_tier,
        "fits_somewhere": bool(possible),
        "fits_with_strict_structured_outputs": bool(usable),
        "reason_code": reason_code,
        "conclusion": conclusion,
        "never": (
            "the canonical prompt is not shortened to fit a quota. Every answer would then be an "
            "answer to a different method, and nothing in the output would show it"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    measurement = measure()
    verdict = assess(measurement)
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "canonical": str(CANONICAL_PATH.relative_to(REPO_ROOT)),
        **measurement.summary,
        "against_published_free_tiers": verdict,
        "note": (
            "estimated, not tokenised by the provider. CJK counted at roughly one token per "
            "character, which is the pessimistic direction for this prompt"
        ),
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if verdict["fits_with_strict_structured_outputs"] else 1

    print(f"canonical v5.1            {measurement.canonical_bytes:>9,} bytes")
    print(f"canonical only            {measurement.canonical_only:>9,} tokens (estimated)")
    print(
        f"canonical + {measurement.addenda_files} addenda     "
        f"{measurement.canonical_plus_addenda:>9,} tokens (estimated)"
    )
    print(f"minimum Stage 3 request   {measurement.minimum_stage3_request:>9,} tokens (no data)")
    print(f"minimum entry request     {measurement.minimum_entry_request:>9,} tokens (no data)")
    print()
    for tier in verdict["tiers"]:
        strict = tier["limits"].get("strict_structured_outputs")
        strict_text = {True: "yes", False: "no", None: "unverified"}[strict]
        print(f"{tier['model_family']}")
        print(
            f"    tokens/minute {tier['limits'].get('tokens_per_minute'):>8,}"
            f"    requests/day {tier['limits'].get('requests_per_day')}"
            f"    strict structured outputs: {strict_text}"
        )
        print(f"    fits: {tier['fits']}  - {tier['why']}")
        if tier["fits"] and tier.get("requests_per_day_at_this_size") is not None:
            print(
                f"    at this size: {tier['requests_per_day_at_this_size']:,} requests/day on "
                "tokens alone"
            )
        print()

    print(f"conclusion: {verdict['conclusion']}")
    if verdict["reason_code"]:
        print(f"            {verdict['reason_code']}")
    print(f"never:      {verdict['never']}")
    return 0 if verdict["fits_with_strict_structured_outputs"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
