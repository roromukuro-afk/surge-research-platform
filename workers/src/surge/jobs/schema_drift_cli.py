"""`python -m surge.jobs.schema_drift_cli` - does the real database match git?

The rule is that ``supabase/migrations/`` is the only source of truth for the
schema (CLAUDE.md §4). The test suite checks that against the database CI builds
*from those files*, which can only ever catch a parsing bug. This is the same
comparison pointed at whichever database ``SURGE_DATABASE_URL`` names - which in
practice means the cloud project, the one place the two can actually disagree.

Read-only: it opens a read-only transaction, reads ``pg_proc``, and rolls back.

Exit code 0 when the schema matches, 1 when it does not, 2 when the question
could not be asked - a database that cannot be reached is not a database that
agrees.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from surge.runtime.schema_drift import compare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("SURGE_DATABASE_URL"),
        help="connection string; defaults to SURGE_DATABASE_URL",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("no database to check: set SURGE_DATABASE_URL or pass --dsn", file=sys.stderr)
        return 2

    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is not installed; install workers[dev]", file=sys.stderr)
        return 2

    try:
        conn = psycopg2.connect(args.dsn)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"could not connect: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        conn.set_session(readonly=True, autocommit=False)
        report = compare(conn)
    finally:
        conn.rollback()
        conn.close()

    if args.json:
        print(
            json.dumps(
                {
                    "in_sync": report.in_sync,
                    "examined": report.examined,
                    "defined_in_migrations": report.defined_in_migrations,
                    "drifted": list(report.drifted),
                    "unmanaged": list(report.unmanaged),
                },
                indent=2,
            )
        )
    else:
        print(report.summary())
        if report.drifted:
            print()
            print("The migrations are the source of truth. Re-apply each of these from its file:")
            for name in report.drifted:
                print(f"  {name}")
        if report.unmanaged:
            print()
            print("In the database and in no migration. Add the migration, or drop the object:")
            for name in report.unmanaged:
                print(f"  {name}")

    return 0 if report.in_sync else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
