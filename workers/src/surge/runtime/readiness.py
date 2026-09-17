"""One command that answers: can this market go live, and if not, why not.

The answer is per market, because the markets are not blocked on the same
things. JP has a live disclosure feed and no price source; US has neither
settled but is not waiting on the JP questions. Reporting a single
system-wide verdict would hold the whole thing at the pace of its worst market -
and would hide the fact that one of them is nearly ready.

Three verdicts:

``LIVE_READY``
    Every check a market needs to produce formal predictions passes.
``PARTIAL_LIVE``
    Some pipeline can run for real - materials, say - while the rest cannot.
    This is a real state and worth naming: JP timely disclosures have been
    live-verified for some time while no JP price exists.
``BLOCKED``
    Nothing can run against real data.

Every check reports what it actually found rather than a bare boolean, because
"price provider: false" and "price provider: Alpaca Basic is IEX-only, which is
not the session high" are different findings and only one of them tells you what
to do next.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

READINESS_VERSION = "readiness-1.1.0"


class Verdict(StrEnum):
    LIVE_READY = "LIVE_READY"
    PARTIAL_LIVE = "PARTIAL_LIVE"
    BLOCKED = "BLOCKED"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: True and worth knowing, but not a gate. A warning never blocks.
    WARN = "WARN"


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str
    #: Whether a formal prediction can be produced without this. A check that
    #: gates nothing is information, not a blocker.
    gates_predictions: bool = True
    #: Whether this check passing means some real pipeline is actually running
    #: against real data. "The teacher tables are still empty" is a true and
    #: useful thing to report and it is not a capability - treating it as one
    #: would make a market with nothing connected look partly live.
    is_a_capability: bool = False
    blocker_id: str | None = None

    @property
    def blocks(self) -> bool:
        return self.status is CheckStatus.FAIL and self.gates_predictions


@dataclass
class MarketReadiness:
    market_code: str
    checks: list[Check] = field(default_factory=list)
    version: str = READINESS_VERSION

    @property
    def blockers(self) -> list[Check]:
        return [check for check in self.checks if check.blocks]

    @property
    def passing(self) -> list[Check]:
        return [check for check in self.checks if check.status is CheckStatus.PASS]

    @property
    def verdict(self) -> Verdict:
        if not self.blockers:
            return Verdict.LIVE_READY
        # PARTIAL_LIVE means something is genuinely running on real data, not
        # merely that some check passed.
        if any(
            check.status is CheckStatus.PASS and check.is_a_capability for check in self.checks
        ):
            return Verdict.PARTIAL_LIVE
        return Verdict.BLOCKED

    @property
    def blocker_ids(self) -> list[str]:
        return sorted({c.blocker_id for c in self.blockers if c.blocker_id})

    @property
    def summary(self) -> dict:
        return {
            "market": self.market_code,
            "verdict": self.verdict.value,
            "blockers": [
                {"check": c.name, "why": c.detail, "decision": c.blocker_id} for c in self.blockers
            ],
            "blocker_ids": self.blocker_ids,
            "checks": {c.name: c.status.value for c in self.checks},
            "version": self.version,
        }


@dataclass
class ReadinessReport:
    markets: list[MarketReadiness] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def any_market_live(self) -> bool:
        return any(m.verdict is Verdict.LIVE_READY for m in self.markets)

    @property
    def summary(self) -> dict:
        return {
            "markets": {m.market_code: m.summary for m in self.markets},
            "any_market_live": self.any_market_live,
            "notes": list(self.notes),
        }

    def render(self) -> str:
        lines: list[str] = []
        for market in self.markets:
            lines.append(f"{market.market_code}: {market.verdict.value}")
            for check in market.checks:
                mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "WARN": " warn "}[check.status.value]
                gate = "" if check.gates_predictions else "  (not a gate)"
                decision = f"  [{check.blocker_id}]" if check.blocker_id else ""
                lines.append(f"{mark} {check.name}{gate}{decision}")
                lines.append(f"        {check.detail}")
            lines.append("")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------- the checks


SQL_CHECKS: dict[str, str] = {
    "security_master_freshness": """
        select max(available_at)::text
        from pipeline.source_fetches f
        join pipeline.runs r using (run_id)
        where r.market_code = %(market)s
    """,
    "universe_authoritative_run": """
        select universe.authoritative_run_at(
            %(market)s::ref.market_code, %(universe_version)s, now()
        )::text
    """,
    "teacher_rows": """
        select (select count(*) from labels.objective_labels)
             + (select count(*) from labels.interpretive_labels)
             + (select count(*) from labels.pipeline_miss_records)
             + (select count(*) from labels.datasets)
    """,
    "mock_cannot_predict": """
        select count(*) from pg_constraint
        where conname = 'predictions_not_from_a_mock'
    """,
    "live_news_sources": """
        select count(*) from news.sources
        where enabled = true and live_verified_at is not null and scope::text = %(market)s
    """,
}


def check_teacher_still_zero(count: int) -> Check:
    """The 0-start, asserted rather than assumed.

    A fixture or a smoke insert into the production teacher tables would be
    invisible afterwards - it would just look like early data - so the check is
    that there is none at all.
    """

    if count == 0:
        return Check(
            name="teacher_rows_still_zero",
            status=CheckStatus.PASS,
            detail=(
                "no production teacher rows, which is correct. The first one comes from a real "
                "episode whose outcome was decided, not from a fixture or a replay"
            ),
            gates_predictions=False,
        )
    return Check(
        name="teacher_rows_still_zero",
        status=CheckStatus.WARN,
        detail=(
            f"{count} production teacher row(s) exist. If production has not started, something "
            "synthetic has been written into the teacher tables and it will be indistinguishable "
            "from real data later"
        ),
        gates_predictions=False,
    )


def check_mock_guard(constraint_count: int) -> Check:
    if constraint_count >= 1:
        return Check(
            name="mock_cannot_produce_a_prediction",
            status=CheckStatus.PASS,
            detail="the database refuses a prediction whose provider kind is DETERMINISTIC_MOCK",
            gates_predictions=False,
        )
    return Check(
        name="mock_cannot_produce_a_prediction",
        status=CheckStatus.FAIL,
        detail=(
            "the constraint that stops a stand-in provider producing a formal prediction is "
            "missing. Nothing may go live without it"
        ),
    )


def check_provider(
    name: str,
    *,
    configured: bool,
    detail: str,
    blocker_id: str | None,
    gates_predictions: bool = True,
    is_a_capability: bool = False,
) -> Check:
    return Check(
        name=name,
        status=CheckStatus.PASS if configured else CheckStatus.FAIL,
        detail=detail,
        gates_predictions=gates_predictions,
        is_a_capability=is_a_capability,
        blocker_id=None if configured else blocker_id,
    )


@dataclass(frozen=True)
class MarketInputs:
    """What the caller could determine about one market.

    Deliberately explicit rather than sniffed from the environment: a readiness
    report that guesses is a readiness report that can be wrong in the
    reassuring direction.
    """

    market_code: str
    #: The consolidated session series the outcome engine reads. For the US this
    #: is D-103-EOD, which Alpaca's delayed SIP history may be able to answer.
    eod_price_provider: str | None = None
    #: A price that could actually have been traded at the moment of a decision.
    #: For the US this is D-103-LIVE and it is a different question with a
    #: different answer: a feed that is deliberately fifteen minutes old cannot
    #: supply an entry_reference_price, however good it is for history.
    intraday_price_provider: str | None = None
    fx_provider: str | None = None
    material_sources_live: int = 0
    #: Stage 3. Produces setups and watches, never an entry.
    eod_analysis_provider: str | None = None
    eod_analysis_is_a_stand_in: bool = True
    #: The intraday contract. This is the one a formal prediction comes from, so
    #: a connected Stage 3 on its own must never read as ready.
    entry_analysis_provider: str | None = None
    entry_analysis_is_a_stand_in: bool = True
    scheduler_configured: bool = False
    object_store_configured: bool = False
    security_master_fresh: bool = False
    universe_run_published: bool = False
    teacher_row_count: int = 0
    mock_guard_constraints: int = 0
    migrations_in_sync: bool = False
    ci_head_green: bool = False


def assess(inputs: MarketInputs) -> MarketReadiness:
    """Turn what is known about one market into a verdict."""

    checks: list[Check] = [
        check_provider(
            "security_master_freshness",
            configured=inputs.security_master_fresh,
            detail=(
                "the security master has a recent snapshot for this market"
                if inputs.security_master_fresh
                else "no recent security master snapshot; identities would be resolved against a "
                "stale listing set"
            ),
            blocker_id=None,
        ),
        check_provider(
            "universe_authoritative_run",
            configured=inputs.universe_run_published,
            detail=(
                "a published universe run answers eligibility as of now"
                if inputs.universe_run_published
                else "no published universe run; nothing can be decided as INCLUDED, and only INCLUDED "
                "may become a prediction"
            ),
            blocker_id=None,
        ),
        check_provider(
            "eod_price_provider",
            configured=bool(inputs.eod_price_provider),
            detail=(
                f"{inputs.eod_price_provider}"
                if inputs.eod_price_provider
                else "no settled end-of-day price source. Without consolidated session OHLC there "
                "is no outcome resolution, because the path ladder reads the session high and low"
            ),
            blocker_id="D-102" if inputs.market_code == "JP" else "D-103-EOD",
            is_a_capability=True,
        ),
        check_provider(
            "intraday_entry_price_provider",
            configured=bool(inputs.intraday_price_provider),
            detail=(
                f"{inputs.intraday_price_provider}"
                if inputs.intraday_price_provider
                else "no intraday source, so no entry_reference_price can be observed and no formal "
                "prediction can be made. A delayed historical feed does not answer this: a price "
                "from fifteen minutes ago is not a price a decision could have been taken at"
            ),
            blocker_id="D-06b" if inputs.market_code == "JP" else "D-103-LIVE",
        ),
        check_provider(
            "fx_provider",
            configured=bool(inputs.fx_provider) or inputs.market_code == "JP",
            detail=(
                "not needed: a yen market needs no conversion for the 3,000 yen filter"
                if inputs.market_code == "JP"
                else (inputs.fx_provider or "no FX source; the 3,000 yen eligibility filter cannot "
                      "be applied to a non-yen price")
            ),
            blocker_id=None if inputs.market_code == "JP" else "D-02a",
        ),
        Check(
            name="material_sources",
            status=CheckStatus.PASS if inputs.material_sources_live else CheckStatus.FAIL,
            detail=(
                f"{inputs.material_sources_live} live-verified source(s)"
                if inputs.material_sources_live
                else "no live-verified material source for this market"
            ),
            #: Materials alone do not make a prediction, but they do make a real
            #: pipeline - which is what PARTIAL_LIVE means.
            gates_predictions=False,
            is_a_capability=True,
        ),
        check_provider(
            "eod_analysis_provider",
            configured=(
                bool(inputs.eod_analysis_provider) and not inputs.eod_analysis_is_a_stand_in
            ),
            detail=(
                f"{inputs.eod_analysis_provider} (Stage 3)"
                if inputs.eod_analysis_provider and not inputs.eod_analysis_is_a_stand_in
                else "the only Stage 3 provider is the deterministic stand-in. Its verdicts are "
                "evidence the pipeline runs and no evidence about any security"
            ),
            blocker_id="D-32-EOD",
        ),
        check_provider(
            "entry_analysis_provider",
            configured=(
                bool(inputs.entry_analysis_provider) and not inputs.entry_analysis_is_a_stand_in
            ),
            detail=(
                f"{inputs.entry_analysis_provider} (intraday entry contract)"
                if inputs.entry_analysis_provider and not inputs.entry_analysis_is_a_stand_in
                else "no real intraday entry analysis. Stage 3 cannot produce an entry - it has no "
                "ENTRY state and runs against a closed market - so connecting a model there leaves "
                "this unanswered. A formal prediction comes only from the intraday contract"
            ),
            blocker_id="D-32-ENTRY",
        ),
        check_provider(
            "scheduler",
            configured=inputs.scheduler_configured,
            detail=(
                "a runner is configured to invoke the daily jobs"
                if inputs.scheduler_configured
                else "no scheduler bound; jobs would only run when someone remembers"
            ),
            blocker_id="D-104",
        ),
        Check(
            name="object_store",
            status=CheckStatus.PASS if inputs.object_store_configured else CheckStatus.WARN,
            detail=(
                "an object store is configured"
                if inputs.object_store_configured
                else "no object store. A local store is sufficient to start, so this is not a gate "
                "(D-105 is not a blocker)"
            ),
            gates_predictions=False,
        ),
        check_teacher_still_zero(inputs.teacher_row_count),
        check_mock_guard(inputs.mock_guard_constraints),
        Check(
            name="migrations_in_sync",
            status=CheckStatus.PASS if inputs.migrations_in_sync else CheckStatus.FAIL,
            detail=(
                "the cloud schema matches supabase/migrations"
                if inputs.migrations_in_sync
                else "the cloud schema and the migrations in git disagree; the migrations are the "
                "only source of truth for the schema"
            ),
        ),
        Check(
            name="ci_head_green",
            status=CheckStatus.PASS if inputs.ci_head_green else CheckStatus.FAIL,
            detail=(
                "CI is green at HEAD"
                if inputs.ci_head_green
                else "CI is not green at HEAD"
            ),
        ),
    ]

    return MarketReadiness(market_code=inputs.market_code, checks=checks)


def assess_all(markets: Sequence[MarketInputs]) -> ReadinessReport:
    report = ReadinessReport(markets=[assess(inputs) for inputs in markets])

    if not report.any_market_live:
        report.notes.append(
            "no market is live. Each blocker names the decision that would clear it; read those "
            "before concluding anything about what is left. An earlier version of this note "
            "asserted that every remaining blocker was a contract or a credential, which was "
            "wrong - D-103 was two questions wearing one id, and one of them had an adapter "
            "waiting to be written"
        )
    partial = [m.market_code for m in report.markets if m.verdict is Verdict.PARTIAL_LIVE]
    if partial:
        report.notes.append(
            f"{', '.join(partial)} can run part of the pipeline for real. That is worth saying "
            "separately from BLOCKED: material collection is live and producing records"
        )
    return report


def collect(conn, market_code: str, *, universe_version: str = "universe-1.0.0", **overrides):
    """Read what the database can answer, and let the caller supply the rest.

    The provider questions are not things the database knows. It knows whether a
    universe run is published and whether the teacher tables are empty; whether
    anyone has settled on a price source is a fact about the world.
    """

    values: dict[str, object] = {}
    with conn.cursor() as cur:
        for name, sql in SQL_CHECKS.items():
            try:
                cur.execute(sql, {"market": market_code, "universe_version": universe_version})
                row = cur.fetchone()
                values[name] = row[0] if row else None
            except Exception as exc:  # noqa: BLE001 - an unreadable check is a failed check
                values[name] = None
                values[f"{name}_error"] = f"{type(exc).__name__}: {exc}"

    inputs = MarketInputs(
        market_code=market_code,
        security_master_fresh=values.get("security_master_freshness") is not None,
        universe_run_published=values.get("universe_authoritative_run") is not None,
        teacher_row_count=int(values.get("teacher_rows") or 0),
        mock_guard_constraints=int(values.get("mock_cannot_predict") or 0),
        material_sources_live=int(values.get("live_news_sources") or 0),
        **overrides,
    )
    return assess(inputs)


__all__ = [
    "READINESS_VERSION",
    "Check",
    "CheckStatus",
    "MarketInputs",
    "MarketReadiness",
    "ReadinessReport",
    "Verdict",
    "assess",
    "assess_all",
    "collect",
]
