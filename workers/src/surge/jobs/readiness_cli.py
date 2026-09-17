"""`python -m surge.jobs.readiness_cli` - can this market go live, and if not why.

Connects when ``SURGE_DATABASE_URL`` is set and asks the database the questions
the database can answer: whether the security master is fresh, whether a
universe run is published, whether the teacher tables are still empty, which
providers are bound, and - separately - whether any of those bindings has ever
produced a row. The rest comes from flags, because whether anyone has *settled*
on a price provider is a fact about the world rather than a row in a table.

Earlier this looked at the connection string and never opened it, which made the
report a description of the flags it was given. Noticing that a variable is set
is not the same as asking.

Exit code is 0 when at least one market is LIVE_READY, and 1 otherwise, so a
scheduler can use it as a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from surge.runtime.readiness import (
    MarketInputs,
    MarketReadiness,
    ReadinessReport,
    Verdict,
    assess,
    collect,
)


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


#: What a flag may answer. Everything else in MarketInputs is the database's to
#: fill, and passing it here would let a command line overrule a measurement.
def _overrides_for(market: str, args) -> dict:
    prefix = market.lower()
    return {
        "eod_price_provider": getattr(args, f"{prefix}_eod", None),
        "intraday_price_provider": getattr(args, f"{prefix}_intraday", None),
        "fx_provider": args.fx,
        "eod_analysis_provider": args.eod_analysis_provider,
        "eod_analysis_is_a_stand_in": not args.eod_analysis_is_real,
        "entry_analysis_provider": args.entry_analysis_provider,
        "entry_analysis_is_a_stand_in": not args.entry_analysis_is_real,
        "scheduler_configured": args.scheduler,
        "object_store_configured": args.object_store,
        "ci_head_green": args.ci_green,
    }


def _inputs_for(market: str, args) -> MarketInputs:
    prefix = market.lower()
    return MarketInputs(
        market_code=market,
        eod_price_provider=getattr(args, f"{prefix}_eod", None),
        intraday_price_provider=getattr(args, f"{prefix}_intraday", None),
        fx_provider=args.fx,
        eod_analysis_provider=args.eod_analysis_provider,
        eod_analysis_is_a_stand_in=not args.eod_analysis_is_real,
        entry_analysis_provider=args.entry_analysis_provider,
        entry_analysis_is_a_stand_in=not args.entry_analysis_is_real,
        scheduler_configured=args.scheduler,
        object_store_configured=args.object_store,
        # Never claimed from a flag. Whether the schema matches git is read
        # from the database itself in collect(); with no connection the
        # honest answer is that nobody looked.
        migrations_in_sync=False,
        migrations_drift_detail=(
            "no database connection, so the schema was never compared with supabase/migrations"
        ),
        ci_head_green=args.ci_green,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markets", default="JP,US")
    parser.add_argument("--jp-eod", default=None, help="settled JP end-of-day price provider")
    parser.add_argument("--jp-intraday", default=None)
    parser.add_argument("--us-eod", default=None)
    parser.add_argument("--us-intraday", default=None)
    parser.add_argument("--fx", default=None)
    parser.add_argument(
        "--eod-analysis-provider",
        default="deterministic_mock",
        help="the Stage 3 provider: produces setups and watches, never an entry",
    )
    parser.add_argument(
        "--eod-analysis-is-real",
        action="store_true",
        help="assert the Stage 3 provider is not the deterministic stand-in",
    )
    parser.add_argument(
        "--entry-analysis-provider",
        default="deterministic_mock",
        help=(
            "the intraday entry provider. This is the one a formal prediction comes from; a "
            "connected Stage 3 does not answer it"
        ),
    )
    parser.add_argument(
        "--entry-analysis-is-real",
        action="store_true",
        help="assert the intraday entry provider is not the deterministic stand-in",
    )
    parser.add_argument("--scheduler", action="store_true")
    parser.add_argument("--object-store", action="store_true")
    parser.add_argument("--ci-green", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    database_url = os.environ.get("SURGE_DATABASE_URL")

    notes: list[str] = []
    markets_readiness: list[MarketReadiness] = []

    if database_url:
        try:
            import psycopg2
        except ImportError:  # pragma: no cover - depends on the environment
            psycopg2 = None

        if psycopg2 is None:
            notes.append(
                "SURGE_DATABASE_URL is set and psycopg2 is not installed, so the database checks "
                "could not be run. They are reported as failing rather than assumed to pass"
            )
            markets_readiness = [assess(_inputs_for(m, args)) for m in markets]
        else:
            connection = psycopg2.connect(database_url)
            try:
                # Read-only, and explicitly so. A readiness report has no
                # business being able to change what it is reporting on.
                connection.set_session(readonly=True, autocommit=True)
                for market in markets:
                    markets_readiness.append(
                        collect(connection, market, **_overrides_for(market, args))
                    )
            finally:
                connection.close()
            notes.append(
                "the database was read for the checks it can answer: security master freshness, "
                "the authoritative universe run, teacher row count, the mock prediction guard, "
                "live material sources, provider role bindings, and - separately from the "
                "bindings - whether any market data or FX row has actually been observed"
            )
    else:
        notes.append(
            "no database connection was supplied, so the checks that read the database "
            "(security master, universe run, teacher row count, mock guard, provider bindings, "
            "observed rows) were not run. They are reported as failing rather than assumed to pass"
        )
        markets_readiness = [assess(_inputs_for(m, args)) for m in markets]

    report = ReadinessReport(markets=markets_readiness)
    if not report.any_market_live:
        report.notes.append(
            "no market is live. Each blocker names the decision that would clear it, and a check "
            "reading BOUND_NOT_LIVE_OBSERVED is implemented and bound and has never fetched "
            "anything - which needs a first run rather than a decision"
        )
    partial = [m.market_code for m in report.markets if m.verdict is Verdict.PARTIAL_LIVE]
    if partial:
        report.notes.append(
            f"{', '.join(partial)} can run part of the pipeline for real"
        )
    report.notes.extend(notes)

    sha = _git_sha()
    if sha:
        report.notes.append(f"HEAD is {sha[:12]}")

    print(json.dumps(report.summary, indent=2) if args.json else report.render())
    return 0 if report.any_market_live else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
