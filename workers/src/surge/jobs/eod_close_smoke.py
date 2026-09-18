"""`python -m surge.jobs.eod_close_smoke` - can the confirmed session close be read?

    --market JP --symbols 7203,9432 --date 2026-09-17   Yahoo, no credential needed
    --market US --symbols AAPL,SPY --date 2026-09-17    Alpaca SIP daily bar, through
                                                        ops\\windows\\Invoke-WithSurgeSecrets.ps1

Prints, per symbol, the close, the session end, when it was read and whether it
counts as confirmed (read after the session end plus the feed's delay), with
the provenance that would be stored. Stores nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def _read(market: str, symbol: str, session_date: date):
    if market == "JP":
        from surge.providers.yahoo_finance import session_close, tse_symbol

        return session_close(tse_symbol(symbol), session_date)
    from surge.providers.alpaca_historical import session_close

    return session_close(symbol, session_date)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--market", choices=("JP", "US"), required=True)
    parser.add_argument("--symbols", required=True, help="JPX local codes for JP, tickers for US")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="the session, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from surge.entry.session_close import SessionCloseNotConfirmed, SessionCloseUnavailable

    rows = []
    for symbol in (s.strip().upper() for s in args.symbols.split(",") if s.strip()):
        row: dict = {"market": args.market, "symbol": symbol, "session_date": args.date.isoformat()}
        try:
            close = _read(args.market, symbol, args.date)
        except SessionCloseNotConfirmed as exc:
            row["result"] = f"not confirmed yet: {exc}"
        except (SessionCloseUnavailable, ValueError) as exc:
            row["result"] = f"unavailable: {exc}"
        else:
            row.update({
                "close": f"{close.close} {close.currency}",
                "session_closed_at": close.session_closed_at.isoformat(),
                "fetched_at": close.fetched_at.isoformat(),
                "confirmed_from": close.confirmed_from.isoformat(),
                "stored evidence": list(close.recorded_evidence),
                "result": "confirmed",
            })
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for row in rows:
            print()
            for key, value in row.items():
                shown = ", ".join(value) if isinstance(value, list) else value
                print(f"  {key:18} {shown}")
    return 0 if all(r["result"] == "confirmed" for r in rows) else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
