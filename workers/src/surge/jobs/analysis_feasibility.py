"""`python -m surge.jobs.analysis_feasibility` - can a free tier run this analysis.

Answerable today, with no credential and no account, because the size of the
input is a property of this repository rather than of any provider. Canonical
v5.1 is registered and hashed; the addenda are files; the bundle's fixed
scaffolding is code. What a request costs is therefore measurable now, and
comparing it against a provider's *published* limits decides whether an account
is worth opening at all.

Three honesty constraints shape the output.

**Exact where exact is possible.** The GPT-OSS models are tokenised with
``o200k_harmony``, which OpenAI publishes, so those numbers are counted rather
than estimated. Families whose tokeniser is not published keep the estimate, and
the report says which is which - a number labelled exact has to be exact.

**The estimate is an estimate.** It treats a CJK character as roughly one token
and Latin text as roughly one per three and a half characters, deliberately the
pessimistic direction for a Japanese prompt. Where both numbers exist they are
shown together: agreement is evidence the estimate can be trusted for the
providers that publish nothing.

**The completion counts.** A tier that meters one combined tokens-per-minute
figure spends the model's own output from the same allowance as the prompt, so
the reserve is added before the comparison.

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
    DEFAULT_COMPLETION_RESERVE,
    PUBLISHED_FREE_LIMITS,
    estimate_tokens,
)
from surge.analysis.llm import render_prompt
from surge.analysis.tokenizer import Count, exact_tokens_or_none

REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_PATH = REPO_ROOT / "docs" / "prompts" / "short-surge-v5.1.original.md"
ADDENDA_DIR = REPO_ROOT / "docs" / "prompts" / "addenda"

CUTOFF = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
DIGEST = "0" * 64


@dataclass(frozen=True)
class Measurement:
    canonical_only: Count
    canonical_plus_addenda: Count
    minimum_stage3_request: Count
    minimum_entry_request: Count
    canonical_bytes: int
    addenda_files: int

    @property
    def floor(self) -> int:
        """The largest of the two minimum requests, on the safest reading."""

        return max(self.minimum_stage3_request.worst, self.minimum_entry_request.worst)

    @property
    def summary(self) -> dict:
        return {
            "canonical_only_tokens": self.canonical_only.summary,
            "canonical_plus_addenda_tokens": self.canonical_plus_addenda.summary,
            "minimum_stage3_request_tokens": self.minimum_stage3_request.summary,
            "minimum_entry_request_tokens": self.minimum_entry_request.summary,
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


def _count(text: str, encoding: str | None) -> Count:
    return Count(
        estimated=estimate_tokens(text),
        exact=exact_tokens_or_none(text, encoding=encoding),
        encoding=encoding,
    )


def measure(encoding: str | None = "o200k_harmony") -> Measurement:
    """Measure the request floor, exactly where the tokeniser is published.

    ``encoding`` defaults to the one the GPT-OSS family uses, because that is
    the family whose limit decides the question. A caller measuring for a
    different family passes its encoding, or None to fall back to the estimate.
    """

    canonical = CANONICAL_PATH.read_text(encoding="utf-8")
    addenda = _addenda_texts()
    return Measurement(
        canonical_only=_count(canonical, encoding),
        canonical_plus_addenda=_count(canonical + "\n".join(addenda), encoding),
        minimum_stage3_request=_count(_minimum_stage3_prompt(canonical, addenda), encoding),
        minimum_entry_request=_count(_minimum_entry_prompt(canonical, addenda), encoding),
        canonical_bytes=len(canonical.encode("utf-8")),
        addenda_files=len(addenda),
    )


def assess_tier(
    measurement: Measurement,
    name: str,
    limits: dict,
    *,
    reserved_output_tokens: int = DEFAULT_COMPLETION_RESERVE,
) -> dict:
    """Compare the floor against one published tier, and name the consequence."""

    tpm = limits.get("tokens_per_minute")
    tpd = limits.get("tokens_per_day")
    floor = measurement.floor
    combined = floor + reserved_output_tokens

    verdict: dict = {
        "model_family": name,
        "limits": {k: (v.value if hasattr(v, "value") else v) for k, v in limits.items()},
        "largest_minimum_request": floor,
        "reserved_output_tokens": reserved_output_tokens,
        "request_including_reserve": combined,
        "measured_exactly": measurement.minimum_entry_request.is_exact,
    }

    if tpm is not None and combined > tpm:
        verdict["fits"] = False
        verdict["reason_code"] = ANALYSIS_FREE_QUOTA_BLOCKED
        verdict["why"] = (
            f"the smallest possible request is {floor:,} tokens, and with a "
            f"{reserved_output_tokens:,} token completion reserve it needs {combined:,} against "
            f"this family's {tpm:,} per minute. One request cannot be sent at all, whatever the "
            "pacing"
        )
        return verdict

    verdict["fits"] = True
    if tpm:
        verdict["requests_per_minute_at_this_size"] = max(1, tpm // combined)
    if tpd:
        verdict["requests_per_day_at_this_size"] = tpd // combined
    if limits.get("requests_per_day") is not None:
        verdict["requests_per_day_cap"] = limits["requests_per_day"]
    verdict["why"] = (
        f"{combined:,} tokens including the reserve fits this family's {tpm:,} per minute"
        if tpm
        else "no per-minute limit is published for this family"
    )
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
        if tier["fits"] and tier["limits"].get("output_mode") == "STRICT_JSON_SCHEMA"
    ]
    possible = [tier for tier in per_tier if tier["fits"]]

    if usable:
        conclusion = (
            "a free family can carry this analysis with strict structured outputs: "
            + ", ".join(t["model_family"] for t in usable)
        )
        reason_code = None
    elif possible:
        conclusion = (
            "no free family both fits and is documented for json_schema. "
            + ", ".join(t["model_family"] for t in possible)
            + " fits on size and is a documented absence from the structured-outputs list, so it "
            "would run on json_object with the contract checked on this side and a bounded retry"
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
        return 0 if verdict["fits_somewhere"] else 1

    def line(label: str, count) -> None:
        mark = "exact" if count.is_exact else "  est"
        both = (
            f"{count.exact:>9,} {mark}   (estimate {count.estimated:,})"
            if count.is_exact
            else f"{count.estimated:>9,} {mark}"
        )
        print(f"{label:<26}{both}")

    print(f"{'canonical v5.1':<26}{measurement.canonical_bytes:>9,} bytes")
    line("canonical only", measurement.canonical_only)
    line(f"canonical + {measurement.addenda_files} addenda", measurement.canonical_plus_addenda)
    line("minimum Stage 3 request", measurement.minimum_stage3_request)
    line("minimum entry request", measurement.minimum_entry_request)
    if measurement.minimum_entry_request.is_exact:
        print(f"{'tokeniser':<26}{measurement.minimum_entry_request.encoding:>9}")
    print()

    for tier in verdict["tiers"]:
        print(f"{tier['model_family']}")
        print(
            f"    tokens/minute {tier['limits'].get('tokens_per_minute'):>8,}"
            f"    requests/day {tier['limits'].get('requests_per_day')}"
            f"    output mode: {tier['limits'].get('output_mode')}"
        )
        if tier["limits"].get("note"):
            print(f"    note: {tier['limits']['note']}")
        print(
            f"    request {tier['largest_minimum_request']:,}"
            f" + reserve {tier['reserved_output_tokens']:,}"
            f" = {tier['request_including_reserve']:,}"
        )
        print(f"    fits: {tier['fits']}  - {tier['why']}")
        if tier["fits"] and tier.get("requests_per_day_at_this_size") is not None:
            print(
                f"    at this size: {tier['requests_per_day_at_this_size']:,} requests/day on "
                f"tokens alone, capped at {tier.get('requests_per_day_cap')} by requests/day"
            )
        print()

    print(f"conclusion: {verdict['conclusion']}")
    if verdict["reason_code"]:
        print(f"            {verdict['reason_code']}")
    print(f"never:      {verdict['never']}")
    return 0 if verdict["fits_somewhere"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
