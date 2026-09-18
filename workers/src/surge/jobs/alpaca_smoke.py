"""`python -m surge.jobs.alpaca_smoke` - does the Alpaca credential work?

Fetches a few days of daily bars for a handful of symbols from the historical
SIP feed, normalises them (which is the validation), reports what came back,
and keeps none of it. The report is counts, dates, labels and a hash - no price
is printed and nothing is written.

The window ends more than fifteen minutes ago: the free plan's historical data
excludes the latest fifteen minutes, and a request that reached into them would
be testing the plan's limit rather than the credential.

Run it with the credentials in the environment - on this machine, through
ops\\windows\\Invoke-WithSurgeSecrets.ps1. The key is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from surge.providers.alpaca_historical import (
    DELAY_MARGIN,
    SIP_DELAY,
    SMOKE_MAX_SESSIONS,
    Credentials,
    credential_smoke,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AAPL,MSFT", help="comma-separated, at most five")
    parser.add_argument("--days", type=int, default=7, help=f"at most {SMOKE_MAX_SESSIONS}")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not Credentials.are_present():
        print(
            "APCA_API_KEY_ID and APCA_API_SECRET_KEY are not both in this process's environment. "
            "Run through ops\\windows\\Invoke-WithSurgeSecrets.ps1 after storing them",
            file=sys.stderr,
        )
        return 2

    now = datetime.now(UTC)
    end = now - SIP_DELAY - DELAY_MARGIN
    start = (end - timedelta(days=args.days)).date()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        report = credential_smoke(symbols, start=start, end=end, now=now)
    except Exception as exc:  # noqa: BLE001 - reported as one line, never a traceback
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = report.summary
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for key in (
            "http_status", "symbols_requested", "symbols_returned", "bars_returned",
            "first_trade_date", "last_trade_date", "requested_feed", "venue_basis",
            "currency", "window_end", "discarded",
        ):
            print(f"  {key:18} {summary[key]}")
        print(f"  {'note':18} {summary['note']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
