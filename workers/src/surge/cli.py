"""Command line entry point for Phase 1 jobs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from surge.config import load_settings
from surge.jobs.universe_sync import run_universe_sync, write_sql_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="surge", description="Phase 1 security master / universe jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("universe-sync", help="Fetch a security master snapshot and classify it")
    sync.add_argument("--market", required=True, choices=["JP", "US"])
    sync.add_argument("--out", required=True, type=Path, help="Directory for generated SQL and summary")
    sync.add_argument("--as-of", type=date.fromisoformat, default=None)
    sync.add_argument("--git-sha", default=None)
    sync.add_argument("--sic-max-pages", type=int, default=60)

    args = parser.parse_args(argv)

    if args.command == "universe-sync":
        settings = load_settings()
        result = run_universe_sync(
            args.market,
            settings=settings,
            as_of=args.as_of,
            sic_max_pages=args.sic_max_pages,
        )
        written = write_sql_artifacts(result, args.out, git_sha=args.git_sha)
        print(
            json.dumps(
                {
                    "run_id": str(result.run_id),
                    "market": result.market_code,
                    "retrieved": len(result.records),
                    "decisions": result.decision_counts,
                    "reasons": result.reason_counts,
                    "provider_errors": result.provider_error_count,
                    "data_quality_warnings": result.data_quality_warning_count,
                    "identity_collision_records": result.identity_collision_record_count,
                    "identity_collision_keys": result.identity_collision_key_count,
                    "files": [str(path) for path in written],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
