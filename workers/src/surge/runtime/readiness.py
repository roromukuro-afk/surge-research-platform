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
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from surge.runtime.schema_drift import compare

READINESS_VERSION = "readiness-1.5.0"


class Verdict(StrEnum):
    LIVE_READY = "LIVE_READY"
    PARTIAL_LIVE = "PARTIAL_LIVE"
    BLOCKED = "BLOCKED"


class Liveness(StrEnum):
    """Three states, because "configured" and "working" are not the same claim.

    The case that forced this: ``FX_USDJPY`` is bound to the ECB, the binding is
    enabled, the adapter is written and tested - and ``market.fx_rates`` holds
    zero rows. Reporting that as a working FX source would be false, and
    reporting it as "no FX source" would throw away the fact that everything
    except the first real fetch is done.
    """

    NOT_BOUND = "NOT_BOUND"
    #: Implemented and bound, and nothing has actually been fetched yet.
    BOUND_NOT_LIVE_OBSERVED = "BOUND_NOT_LIVE_OBSERVED"
    #: Rows exist, from this provider, and the newest is recent enough to act on.
    LIVE_FRESH = "LIVE_FRESH"
    #: Rows exist and the newest is too old. A year-old bar is not a price, and
    #: counting it would let a pipeline that stopped last spring read as working.
    LIVE_STALE = "LIVE_STALE"

    @property
    def is_working(self) -> bool:
        return self is Liveness.LIVE_FRESH


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
    #: For checks that distinguish a binding from an observation.
    liveness: Liveness | None = None

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
            "liveness": {
                c.name: c.liveness.value for c in self.checks if c.liveness is not None
            },
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
    #: A binding is a configuration. It says which provider would be used, and
    #: nothing at all about whether anything has been fetched.
    "price_binding": """
        select provider_id from market.provider_role_bindings
        where enabled = true and effective_to is null
          and role::text = case when %(market)s = 'JP' then 'EOD_CURRENT_JP' else 'EOD_CURRENT_US' end
        order by priority limit 1
    """,
    "fx_binding": """
        select provider_id from market.provider_role_bindings
        where enabled = true and effective_to is null and role::text = 'FX_USDJPY'
        order by priority limit 1
    """,
    #: And these are the observations. Deliberately separate queries: one
    #: answers "is it configured", the other "has it ever produced a row".
    #: Rows *from the bound provider*, with how recent they are. Counting rows
    #: from any provider would let a decommissioned source keep a market looking
    #: live, and counting rows without a date would let a stopped pipeline do the
    #: same. The dataset and market are part of the question, not context.
    "price_freshness": """
        select count(*),
               max(b.trade_date)::text,
               max(b.available_at)::text
          from market.daily_bars b
          join ref.securities s using (security_id)
         where s.market_code = %(market)s
           and b.provider_id = coalesce(%(price_provider)s, b.provider_id)
    """,
    "fx_freshness": """
        select count(*), max(source_date)::text, max(available_at)::text
          from market.fx_rates
         where provider_id = coalesce(%(fx_provider)s, provider_id)
    """,
}

#: How old the newest row may be before a source stops counting as working.
#: Generous, and deliberately not zero: a market closed over a weekend has no
#: new bar on Sunday and is not broken. Three days covers a normal weekend plus
#: one public holiday; anything beyond that is a pipeline that has stopped.
PRICE_MAX_AGE = timedelta(days=3)

#: FX moves every business day and the eligibility filter converts with it, so
#: a stale rate silently mis-prices the 3,000 yen test for a whole market.
FX_MAX_AGE = timedelta(days=3)


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


def check_capability(
    name: str,
    *,
    settled_provider: str | None,
    bound_provider: str | None,
    rows_observed: int,
    what_it_holds: str,
    blocker_id: str | None,
    latest_at: datetime | None = None,
    max_age: timedelta | None = None,
    now: datetime | None = None,
    gates_predictions: bool = True,
) -> Check:
    """One check that separates bound, observed and recent.

    Three different things to do next, so three different answers. An unbound
    role needs a decision; a bound one with no rows needs a first fetch; one
    whose newest row is old needs somebody to find out why the pipeline stopped.
    Collapsing any pair of them hides which.
    """

    provider = settled_provider or bound_provider
    if not provider:
        return Check(
            name=name,
            status=CheckStatus.FAIL,
            detail=f"no provider is bound for this role, and {what_it_holds} is empty",
            gates_predictions=gates_predictions,
            is_a_capability=True,
            blocker_id=blocker_id,
            liveness=Liveness.NOT_BOUND,
        )

    if rows_observed <= 0:
        return Check(
            name=name,
            status=CheckStatus.FAIL,
            detail=(
                f"{provider} is bound and enabled, and {what_it_holds} holds no rows from it. "
                "IMPLEMENTED and BOUND, not yet live observed: the adapter and the binding are "
                "done and nothing has been fetched, so this cannot be reported as a working source"
            ),
            gates_predictions=gates_predictions,
            is_a_capability=True,
            blocker_id=blocker_id,
            liveness=Liveness.BOUND_NOT_LIVE_OBSERVED,
        )

    if latest_at is None:
        # Rows with no date at all. Not treated as fresh: "we cannot tell how old
        # this is" is not evidence that it is new.
        return Check(
            name=name,
            status=CheckStatus.FAIL,
            detail=(
                f"{provider} has {rows_observed:,} row(s) in {what_it_holds} and none of them "
                "carries a date, so how recent the data is cannot be established"
            ),
            gates_predictions=gates_predictions,
            is_a_capability=True,
            blocker_id=blocker_id,
            liveness=Liveness.LIVE_STALE,
        )

    now = now or datetime.now(UTC)
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=UTC)
    age = now - latest_at

    if max_age is not None and age > max_age:
        return Check(
            name=name,
            status=CheckStatus.FAIL,
            detail=(
                f"{provider} has {rows_observed:,} row(s) in {what_it_holds} and the newest is "
                f"{age.days} day(s) old, past the {max_age.days} day limit. Data this old is a "
                "pipeline that stopped, and treating it as a working source would make a market "
                "that went quiet in the spring read as live"
            ),
            gates_predictions=gates_predictions,
            is_a_capability=True,
            blocker_id=blocker_id,
            liveness=Liveness.LIVE_STALE,
        )

    return Check(
        name=name,
        status=CheckStatus.PASS,
        detail=(
            f"{provider}, {rows_observed:,} row(s) in {what_it_holds}, newest "
            f"{age.days} day(s) old"
        ),
        gates_predictions=gates_predictions,
        is_a_capability=True,
        liveness=Liveness.LIVE_FRESH,
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
    #: What the database says is bound, as distinct from what a person settled
    #: on. Both matter and they answer different questions.
    eod_price_binding: str | None = None
    fx_binding: str | None = None
    #: What has actually been fetched. A binding with zero rows is an
    #: implementation, not a source.
    price_rows_observed: int = 0
    fx_rows_observed: int = 0
    #: When the newest row from the bound provider arrived. A count on its own
    #: cannot tell a working pipeline from one that stopped.
    price_latest_at: datetime | None = None
    fx_latest_at: datetime | None = None
    now: datetime | None = None
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
    #: What the comparison actually found, so the report can name the drifted
    #: functions instead of repeating a sentence that is true of every failure.
    migrations_drift_detail: str | None = None
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
        check_capability(
            "eod_price_provider",
            settled_provider=inputs.eod_price_provider,
            bound_provider=inputs.eod_price_binding,
            rows_observed=inputs.price_rows_observed,
            what_it_holds="market.daily_bars",
            blocker_id="D-102" if inputs.market_code == "JP" else "D-103-EOD",
            latest_at=inputs.price_latest_at,
            max_age=PRICE_MAX_AGE,
            now=inputs.now,
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
        (
            Check(
                name="fx_provider",
                status=CheckStatus.PASS,
                detail="not needed: a yen market needs no conversion for the 3,000 yen filter",
                blocker_id=None,
            )
            if inputs.market_code == "JP"
            else check_capability(
                "fx_provider",
                settled_provider=inputs.fx_provider,
                bound_provider=inputs.fx_binding,
                rows_observed=inputs.fx_rows_observed,
                what_it_holds="market.fx_rates",
                blocker_id="D-02a",
                latest_at=inputs.fx_latest_at,
                max_age=FX_MAX_AGE,
                now=inputs.now,
            )
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
            blocker_id="D-189",
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
            blocker_id="D-190",
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
                inputs.migrations_drift_detail
                or (
                    "the schema matches supabase/migrations"
                    if inputs.migrations_in_sync
                    else "the schema and the migrations in git disagree; the migrations are the "
                    "only source of truth for the schema"
                )
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


def _as_datetime(value) -> datetime | None:
    """A date or timestamp from the database, as an aware datetime.

    ``available_at`` is when this system learned the value, which is the right
    clock for "has the pipeline run recently" - a bar's trade_date says when the
    market traded, not when anybody fetched it.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ask(conn, cur, sql, params):
    """Run one check, and let a failure be a failure of that check alone.

    Without this, a single broken query takes the rest of the report with it:
    inside a transaction Postgres refuses every later statement until someone
    rolls back, so the report would show one real finding followed by a dozen
    checks that look equally broken and are not. A savepoint keeps the blast
    radius at one question. In autocommit there is no transaction to poison and
    no savepoint to take.
    """

    savepoint = not conn.autocommit
    if savepoint:
        cur.execute("savepoint readiness_check")
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - an unreadable check is a failed check
        if savepoint:
            cur.execute("rollback to savepoint readiness_check")
        return None, f"{type(exc).__name__}: {exc}"
    if savepoint:
        cur.execute("release savepoint readiness_check")
    return row, None


def collect(conn, market_code: str, *, universe_version: str = "universe-1.0.0", **overrides):
    """Read what the database can answer, and let the caller supply the rest.

    The provider questions split in two. Whether anyone has *settled* on a price
    source is a fact about the world and comes from the caller; whether a
    binding exists and whether it has ever produced a row are facts about the
    database, and both are read here. Keeping them apart is what lets the report
    say "bound, never fetched" instead of collapsing it into either "ready" or
    "nothing here".
    """

    values: dict[str, object] = {}
    errors: list[str] = []

    # The bindings are read first, because the freshness queries are *about* the
    # bound provider. Counting rows from any provider would let a source that was
    # decommissioned last year keep a market looking live.
    bindings: dict[str, object] = {}
    with conn.cursor() as cur:
        for name in ("price_binding", "fx_binding"):
            row, error = _ask(
                conn,
                cur,
                SQL_CHECKS[name],
                {"market": market_code, "universe_version": universe_version,
                 "price_provider": None, "fx_provider": None},
            )
            bindings[name] = row[0] if row else None
            if error:
                errors.append(f"{name}: {error}")

    with conn.cursor() as cur:
        for name, sql in SQL_CHECKS.items():
            row, error = _ask(
                conn,
                cur,
                sql,
                {
                    "market": market_code,
                    "universe_version": universe_version,
                    "price_provider": bindings.get("price_binding"),
                    "fx_provider": bindings.get("fx_binding"),
                },
            )
            values[name] = row if row else None
            if error:
                errors.append(f"{name}: {error}")

    def scalar(name):
        row = values.get(name)
        return row[0] if row else None

    price = values.get("price_freshness") or (0, None, None)
    fx = values.get("fx_freshness") or (0, None, None)

    # Whether the schema matches git is a fact about this database, so it is
    # measured here rather than asserted on a command line. A caller saying the
    # migrations are in sync does not make the bodies agree - the same reason
    # the row counts are not taken from a flag either.
    overrides.pop("migrations_in_sync", None)
    try:
        drift = compare(conn)
        migrations_in_sync = drift.in_sync
        drift_detail = drift.summary()
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is not a yes
        migrations_in_sync = False
        drift_detail = f"the comparison could not be made - {type(exc).__name__}: {exc}"

    inputs = MarketInputs(
        market_code=market_code,
        migrations_in_sync=migrations_in_sync,
        migrations_drift_detail=drift_detail,
        security_master_fresh=scalar("security_master_freshness") is not None,
        universe_run_published=scalar("universe_authoritative_run") is not None,
        teacher_row_count=int(scalar("teacher_rows") or 0),
        mock_guard_constraints=int(scalar("mock_cannot_predict") or 0),
        material_sources_live=int(scalar("live_news_sources") or 0),
        eod_price_binding=bindings.get("price_binding"),
        fx_binding=bindings.get("fx_binding"),
        price_rows_observed=int(price[0] or 0),
        fx_rows_observed=int(fx[0] or 0),
        price_latest_at=_as_datetime(price[2]),
        fx_latest_at=_as_datetime(fx[2]),
        **overrides,
    )
    readiness = assess(inputs)
    for error in errors:
        # An unreadable check is a failed check, and it should say which one.
        readiness.checks.append(
            Check(
                name="database_check_failed",
                status=CheckStatus.FAIL,
                detail=f"a readiness query could not be run, so its answer is unknown - {error}",
            )
        )
    return readiness


__all__ = [
    "READINESS_VERSION",
    "Check",
    "CheckStatus",
    "FX_MAX_AGE",
    "PRICE_MAX_AGE",
    "Liveness",
    "MarketInputs",
    "MarketReadiness",
    "ReadinessReport",
    "Verdict",
    "assess",
    "assess_all",
    "check_capability",
    "collect",
]
