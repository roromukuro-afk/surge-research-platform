"""The TSV width is a contract between the worker and the SQL loader.

The loader reads the snapshot positionally and drops rows whose field count does
not match, without an error and without a counter: a width change that is not
mirrored in the migration loads zero rows and returns 0. Nothing else in the
test suite compares the two sides, so this does.
"""

from __future__ import annotations

import re
from pathlib import Path

from surge.sql_emit import SNAPSHOT_COLUMNS, TSV_COLUMNS, TSV_FIELD_COUNT

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
LOADER = "load_master_snapshot_from_signed_url"
WIDTH_GUARD = re.compile(r"array_length\(f, 1\) = (\d+)")


DEFINITION = f"create or replace function pipeline.{LOADER}"


def _current_loader_sql() -> tuple[Path, str]:
    """The most recent migration that DEFINES the loader, not one that mentions it."""

    candidates = sorted(
        path for path in MIGRATIONS.glob("*.sql") if DEFINITION in path.read_text(encoding="utf-8")
    )
    assert candidates, "no migration defines the signed URL loader"
    latest = candidates[-1]
    return latest, latest.read_text(encoding="utf-8")


def test_loader_expects_exactly_the_emitted_field_count():
    path, sql = _current_loader_sql()
    widths = WIDTH_GUARD.findall(sql)
    assert widths, f"{path.name} defines the loader but has no array_length guard"
    assert {int(width) for width in widths} == {TSV_FIELD_COUNT}, (
        f"{path.name} expects {widths} fields, the worker emits {TSV_FIELD_COUNT}"
    )


def test_loader_reads_every_emitted_position():
    _, sql = _current_loader_sql()
    body = sql[sql.index(LOADER) :]
    for position in range(1, TSV_FIELD_COUNT + 1):
        assert f"f[{position}]" in body, f"the loader never reads field {position}"
    assert f"f[{TSV_FIELD_COUNT + 1}]" not in body, "the loader reads a field the worker does not emit"


def test_run_id_is_passed_separately_not_in_the_file():
    assert "run_id" in SNAPSHOT_COLUMNS
    assert "run_id" not in TSV_COLUMNS
    assert TSV_FIELD_COUNT == len(SNAPSHOT_COLUMNS) - 1
