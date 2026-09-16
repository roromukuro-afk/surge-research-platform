"""Deterministic SQL emission for snapshot loading.

The worker writes SQL rather than talking to the database directly so the same
artefact can be applied through any channel (psql, CI, or the managed API) and
reviewed before it runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

SNAPSHOT_COLUMNS = (
    "run_id",
    "source_id",
    "source_record_id",
    "market_code",
    "exchange_id",
    "local_code",
    "symbol",
    "name",
    "normalized_name",
    "security_type",
    "market_segment_code",
    "market_segment_name",
    "currency",
    "country",
    "is_adr",
    "is_test_issue",
    "is_spac_pre_merger",
    "listing_status",
    "cik",
    "edinet_code",
    "corporate_number",
    "issuer_identity_source",
    "issuer_identity_key",
    "issuer_identity_confidence",
    "security_identity_source",
    "security_identity_key",
    "security_identity_confidence",
    "type_evidence",
    "decision",
    "reason_code",
    "decision_detail",
    "observed_at",
    "available_at",
    "source_data_version",
    # Phase 1.1a. Appended, never inserted in the middle: the SQL loader reads
    # the TSV positionally, so every existing position has to stay where it is.
    "issuer_name",
    "issuer_name_source",
    "identity_version",
    "issuer_normalized_name",
)


class Json:
    """Marks a value that must be rendered as a jsonb literal."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def quote(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, Json):
        return quote(json.dumps(value.value, ensure_ascii=False, sort_keys=True)) + "::jsonb"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, datetime | date):
        return quote(value.isoformat())
    text = str(value).replace("'", "''")
    if "\\" in text:
        return "E'" + text.replace("\\", "\\\\") + "'"
    return f"'{text}'"


def insert_statement(table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], *, on_conflict: str = "") -> str:
    if not rows:
        return ""
    column_list = ", ".join(columns)
    values = ",\n  ".join("(" + ", ".join(quote(value) for value in row) + ")" for row in rows)
    suffix = f" {on_conflict}" if on_conflict else ""
    return f"insert into {table} ({column_list}) values\n  {values}{suffix};"


def chunked(rows: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# Unit separator: cannot occur in issuer or security names, so the delimited
# format needs no quoting and survives Japanese text unchanged.
FIELD_DELIMITER = "\x1f"

# Same order as pipeline.load_master_snapshot_from_signed_url expects, minus
# run_id which is passed to the function. The loader checks the field count and
# silently drops rows of the wrong width, so this number is a contract:
# test_snapshot_width.py asserts it against the migration.
TSV_COLUMNS = tuple(column for column in SNAPSHOT_COLUMNS if column != "run_id")
TSV_FIELD_COUNT = len(TSV_COLUMNS)


def tsv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Json):
        return json.dumps(value.value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    text = str(value)
    # A stray delimiter or newline would silently shift every following column.
    return text.replace(FIELD_DELIMITER, " ").replace("\r", " ").replace("\n", " ")


def tsv_line(row: Sequence[Any]) -> str:
    return FIELD_DELIMITER.join(tsv_value(value) for value in row)
