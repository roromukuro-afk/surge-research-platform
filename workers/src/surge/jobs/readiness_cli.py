"""`python -m surge.jobs.readiness_cli` - can this market go live, and if not why.

Reads what the database can answer and takes the rest as explicit flags, because
whether anyone has settled on a price provider is a fact about the world rather
than a row in a table. Guessing it from the presence of an environment variable
would make the report wrong in the reassuring direction.

Exit code is 0 when at least one market is LIVE_READY, and 1 otherwise, so a
scheduler can use it as a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from surge.runtime.readiness import MarketInputs, ReadinessReport, assess_all


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


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
        migrations_in_sync=args.migrations_in_sync,
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
    parser.add_argument("--migrations-in-sync", action="store_true")
    parser.add_argument("--ci-green", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    report: ReadinessReport = assess_all([_inputs_for(m, args) for m in markets])

    database_url = os.environ.get("SURGE_DATABASE_URL")
    if not database_url:
        report.notes.append(
            "no database connection was supplied, so the checks that read the database "
            "(security master, universe run, teacher row count, mock guard) were not run. "
            "They are reported as failing rather than assumed to pass"
        )

    sha = _git_sha()
    if sha:
        report.notes.append(f"HEAD is {sha[:12]}")

    print(json.dumps(report.summary, indent=2) if args.json else report.render())
    return 0 if report.any_market_live else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
