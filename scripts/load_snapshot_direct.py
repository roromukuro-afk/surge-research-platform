"""Load a universe-sync snapshot straight into Postgres over a direct connection.

This is the credential model Phase 1.1b settles on for a maintenance load: the
operator (or the worker) connects with a credential that lives in a secret
store and streams the file with COPY. Nothing is uploaded anywhere, no signed
URL is minted, and no anonymous storage policy is ever created.

The object storage path (pipeline.load_master_snapshot_from_signed_url) stays
for the case where the machine running the job cannot reach the database, and
it needs a bucket-scoped credential from the same secret store - never an
anonymous policy.

Usage:
    SURGE_DB_URL=postgresql://user:pass@host:5432/postgres \\
    python scripts/load_snapshot_direct.py ./.local/rebuild JP

The DSN is read from the environment and never printed.
"""

from __future__ import annotations

import os
import pathlib
import sys

import psycopg2

# The snapshot layout, minus run_id, which is passed separately.
TSV_COLUMNS = [
    "source_id", "source_record_id", "market_code", "exchange_id", "local_code", "symbol", "name",
    "normalized_name", "security_type", "market_segment_code", "market_segment_name", "currency", "country",
    "is_adr", "is_test_issue", "is_spac_pre_merger", "listing_status", "cik", "edinet_code", "corporate_number",
    "issuer_identity_source", "issuer_identity_key", "issuer_identity_confidence", "security_identity_source",
    "security_identity_key", "security_identity_confidence", "type_evidence", "decision", "reason_code",
    "decision_detail", "observed_at", "available_at", "source_data_version", "issuer_name", "issuer_name_source",
    "identity_version", "issuer_normalized_name",
]
NULLABLE = {
    "exchange_id", "symbol", "market_segment_code", "market_segment_name", "is_spac_pre_merger", "cik",
    "edinet_code", "corporate_number", "issuer_name", "issuer_name_source", "identity_version",
    "issuer_normalized_name",
}
CASTS = {
    "market_code": "ref.market_code", "security_type": "ref.security_type",
    "listing_status": "ref.listing_status", "decision": "universe.decision",
    "is_adr": "boolean", "is_test_issue": "boolean", "is_spac_pre_merger": "boolean",
    "type_evidence": "jsonb", "decision_detail": "jsonb",
    "observed_at": "timestamptz", "available_at": "timestamptz",
}
JSON_DEFAULT = {"type_evidence", "decision_detail"}

BACKSLASH = chr(92)
ESC = chr(69)  # E'' string prefix, written this way to keep the escape out of the source


def copy_statement(sample: str) -> str:
    """text format unless the data contains a backslash, which it would escape."""

    if BACKSLASH in sample:
        assert chr(1) not in sample, "the CSV quote character occurs in the data"
        return (
            "copy stage_raw from stdin with (format csv, delimiter " + ESC + "'\\x1f', "
            "quote " + ESC + "'\\x01', escape " + ESC + "'\\x01')"
        )
    return "copy stage_raw from stdin with (format text, delimiter " + ESC + "'\\x1f')"


def main(out_dir: str, market: str) -> int:
    dsn = os.environ.get("SURGE_DB_URL")
    if not dsn:
        print("set SURGE_DB_URL (it is read from the environment and never printed)", file=sys.stderr)
        return 2

    out = pathlib.Path(out_dir)
    lower = market.lower()
    run_sql = (out / f"{lower}_00_run.sql").read_text(encoding="utf-8")
    run_id = run_sql.split("values\n  ('")[1][:36]
    tsv = out / f"{lower}_snapshot.tsv"

    selects = []
    for column in TSV_COLUMNS:
        expr = f'"{column}"'
        if column in JSON_DEFAULT:
            expr = f"coalesce(nullif({expr}, ''), '{{}}')"
        elif column in NULLABLE:
            expr = f"nullif({expr}, '')"
        cast = CASTS.get(column)
        if cast:
            expr = f"({expr})::{cast}"
        selects.append(expr)

    connection = psycopg2.connect(dsn, connect_timeout=30)
    connection.autocommit = False
    try:
        with connection.cursor() as cur:
            cur.execute(run_sql)  # pipeline.runs, source_fetches, run_errors
            cur.execute(
                "create temporary table stage_raw ("
                + ", ".join(f'"{c}" text' for c in TSV_COLUMNS)
                + ") on commit drop"
            )
            with tsv.open("r", encoding="utf-8", newline="") as handle:
                cur.copy_expert(copy_statement(tsv.read_text(encoding="utf-8")), handle)
            cur.execute("select count(*) from stage_raw")
            staged = cur.fetchone()[0]
            cur.execute(
                f"insert into pipeline.master_snapshot (run_id, {', '.join(TSV_COLUMNS)}) "
                f"select %s::uuid, {', '.join(selects)} from stage_raw "
                "on conflict (run_id, source_id, source_record_id) do nothing",
                (run_id,),
            )
            loaded = cur.rowcount
        connection.commit()
    finally:
        connection.close()

    print(f"{market.upper()} run {run_id}: staged {staged}, loaded {loaded}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
