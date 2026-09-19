"""Leakage checks for one evaluation sample (jev-evaluation-design.md 9-3).

Each check returns the violations it found; an empty list is a pass. Preflight
runs all of them on every sample and refuses to send anything if any fails.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime

from surge.evaluation.outcome import Outcome
from surge.evaluation.population import PRICE_LIMIT_JPY, Sample, Screen
from surge.evaluation.state import REDACTED_CODE, REDACTED_NAME, code_pattern, data_cutoff, name_variants
from surge.providers.yahoo_finance import JST

_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def bars_not_after_s0(state: dict, s0: date) -> list[str]:
    late = [b["date"] for b in state["daily_bars_up_to_s0"] if date.fromisoformat(b["date"]) > s0]
    return [f"state bar dated after S0: {d}" for d in late]


def disclosures_before_cutoff(state: dict, s0: date) -> list[str]:
    cutoff = data_cutoff(s0)
    late = [d["published_at"] for d in state["tdnet_disclosure_titles_up_to_cutoff"]
            if datetime.fromisoformat(d["published_at"]).astimezone(JST) > cutoff]
    return [f"disclosure published after the cutoff: {d}" for d in late]


def no_date_after_cutoff(state: dict, s0: date) -> list[str]:
    """Any calendar date anywhere in the data part of the state, method excluded."""

    data = {k: v for k, v in state.items() if k != "method"}
    limit = data_cutoff(s0).date()
    found = set()
    for match in _ISO_DATE.finditer(json.dumps(data, ensure_ascii=False)):
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if day > limit:
            found.add(day.isoformat())
    return [f"a date after the cutoff appears in the state: {d}" for d in sorted(found)]


def outcome_window_outside_state(state: dict, outcome: Outcome, s0: date) -> list[str]:
    problems = []
    window = [date.fromisoformat(d) for d in outcome.window]
    if any(day <= s0 for day in window):
        problems.append("the outcome window includes S0 or an earlier session")
    state_days = {b["date"] for b in state["daily_bars_up_to_s0"]}
    overlap = sorted(state_days & set(outcome.window))
    if overlap:
        problems.append(f"outcome sessions are in the state: {overlap}")
    return problems


def base_matches(state: dict, outcome: Outcome) -> list[str]:
    from decimal import Decimal

    if Decimal(state["s0"]["confirmed_close_jpy"]) != outcome.base:
        return [f"the state's S0 close {state['s0']['confirmed_close_jpy']} is not the outcome base {outcome.base}"]
    return []


def screened_as_of_s0(screened: Screen, s0: date) -> list[str]:
    problems = []
    if screened.series is None or screened.series.as_of != s0:
        problems.append("the screening series was not built as of S0")
    elif screened.series.bars[-1].trade_date != s0:
        problems.append("the screening series does not end on S0")
    if screened.features is not None and screened.features.trade_date != s0:
        problems.append("the feature row is not S0's")
    return problems


def anonymized_has_no_identity(state: dict, sample: Sample) -> list[str]:
    """No ticker, symbol or company name where identity can enter: the security block and the titles.

    Numbers are not searched for the code: a price or a volume that happens to
    read 1301 is not the ticker 1301.
    """

    problems = []
    if state["security"].get("code") != REDACTED_CODE:
        problems.append("the security code is not redacted")
    if state["security"].get("name") != REDACTED_NAME:
        problems.append("the company name is not redacted")
    text = json.dumps({k: v for k, v in state.items() if k != "method"}, ensure_ascii=False)
    if f"{sample.code}.T" in text:
        problems.append("the Yahoo symbol appears in the anonymized state")
    for variant in name_variants(sample.name):
        if variant in text:
            problems.append(f"a company-name variant appears in the anonymized state ({len(variant)} chars)")
    titles = "\n".join(t["title"] for t in state["tdnet_disclosure_titles_up_to_cutoff"])
    if code_pattern(sample.code).search(titles):
        problems.append("the ticker appears in a disclosure title")
    return problems


def method_matches_manifest(state: dict, canonical_sha256: str, addenda_sha256: list[str]) -> list[str]:
    method = state["method"]
    problems = []
    if hashlib.sha256(method["canonical_v5_1"].encode()).hexdigest() != canonical_sha256:
        problems.append("the canonical text differs from the manifest's")
    if [hashlib.sha256(a.encode()).hexdigest() for a in method["addenda_newer_overrides_older"]] != addenda_sha256:
        problems.append("the addenda differ from the manifest's (content or order)")
    return problems


def eligible_and_not_teacher_data(sample: Sample) -> list[str]:
    problems = []
    if sample.s0_close_as_traded > PRICE_LIMIT_JPY:
        problems.append(f"S0 close {sample.s0_close_as_traded} is above 3,000 yen")
    if sample.teacher_admissible:
        problems.append("a sample is marked teacher-admissible")
    return problems


def check_sample(sample: Sample, screened: Screen, state: dict, outcome: Outcome, *, canonical_sha256: str,
                 addenda_sha256: list[str], anonymized_state: dict | None = None) -> list[str]:
    s0 = sample.s0
    problems = [
        *bars_not_after_s0(state, s0),
        *disclosures_before_cutoff(state, s0),
        *no_date_after_cutoff(state, s0),
        *outcome_window_outside_state(state, outcome, s0),
        *base_matches(state, outcome),
        *screened_as_of_s0(screened, s0),
        *method_matches_manifest(state, canonical_sha256, addenda_sha256),
        *eligible_and_not_teacher_data(sample),
    ]
    if anonymized_state is not None:
        problems += anonymized_has_no_identity(anonymized_state, sample)
        problems += method_matches_manifest(anonymized_state, canonical_sha256, addenda_sha256)
    return problems


def base_is_the_sample_close(state: dict, sample: Sample) -> list[str]:
    from decimal import Decimal

    if Decimal(state["s0"]["confirmed_close_jpy"]) != sample.s0_close_as_traded:
        return [f"the state's S0 close {state['s0']['confirmed_close_jpy']} is not the sample's "
                f"{sample.s0_close_as_traded}"]
    return []


def check_prospective_sample(sample: Sample, screened: Screen, state: dict, *, canonical_sha256: str,
                             addenda_sha256: list[str], anonymized_state: dict | None = None) -> list[str]:
    """Every check of ``check_sample`` that needs no outcome: Phase B asks before its outcome exists.

    The outcome's own checks (its window after S0 and outside the state, its
    base the state's close) run when it is frozen, after T+20.
    """

    s0 = sample.s0
    problems = [
        *bars_not_after_s0(state, s0),
        *disclosures_before_cutoff(state, s0),
        *no_date_after_cutoff(state, s0),
        *base_is_the_sample_close(state, sample),
        *screened_as_of_s0(screened, s0),
        *method_matches_manifest(state, canonical_sha256, addenda_sha256),
        *eligible_and_not_teacher_data(sample),
    ]
    if anonymized_state is not None:
        problems += anonymized_has_no_identity(anonymized_state, sample)
        problems += method_matches_manifest(anonymized_state, canonical_sha256, addenda_sha256)
    return problems


__all__ = ["anonymized_has_no_identity", "bars_not_after_s0", "base_is_the_sample_close", "base_matches",
           "check_prospective_sample", "check_sample", "disclosures_before_cutoff", "eligible_and_not_teacher_data",
           "method_matches_manifest", "no_date_after_cutoff", "outcome_window_outside_state", "screened_as_of_s0"]
