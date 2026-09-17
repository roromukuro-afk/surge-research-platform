"""The smoke renderer writes the production statements, or it writes nothing.

The value of this pass is that it proves the *real* inserts work against the
real schema. That is only true if the SQL it sends is the SQL production sends,
so these tests are mostly about the renderer refusing to invent anything: no
column lists of its own, no silent type coercion, and above all no body text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from surge.jobs.tdnet_smoke import literal, render, render_statement
from surge.news.db import INSERT_DOCUMENT, INSERT_TDNET_ITEM
from test_yanoshin_tdnet import _RESPONSE_TEXT  # noqa: E402

NOW = datetime(2026, 9, 17, 5, 0, tzinfo=UTC)
FETCHED = datetime(2026, 9, 17, 4, 59, tzinfo=UTC)


def test_a_quote_in_a_japanese_title_cannot_close_the_string():
    """The one injection route a title actually has."""

    assert literal("O'Reilly") == "'O''Reilly'"
    assert literal("株式会社'; drop table news.documents; --") == (
        "'株式会社''; drop table news.documents; --'"
    )


def test_a_naive_datetime_is_refused_rather_than_guessed():
    """It would be read in the server's zone, which is not ours."""

    with pytest.raises(ValueError, match="naive datetime"):
        literal(datetime(2026, 9, 17, 5, 0))


def test_a_decimal_keeps_its_own_form_and_an_unknown_type_raises():
    assert literal(Decimal("1.5")) == "1.5"
    with pytest.raises(TypeError):
        literal({"a": 1})


def test_every_placeholder_is_substituted():
    rendered = render_statement(
        INSERT_DOCUMENT,
        {
            key: None
            for key in (
                "source_key,source_document_id,document_url,document_type,title,language,"
                "source_published_at,source_published_precision,system_first_seen_at,ingested_at,"
                "available_to_model_at,availability_basis,replay_assumed_available_at,"
                "replay_assumption_note,content_sha256,raw_object_key,body_storage,"
                "body_storage_reason,body_text,byte_size,run_id,fetch_id,revision_seq,"
                "supersedes_document_id"
            ).split(",")
        },
    )
    assert "%(" not in rendered


def test_a_missing_value_is_an_error_not_an_empty_string():
    """A silently-null column is how a NOT NULL constraint gets discovered in
    production rather than here."""

    with pytest.raises(KeyError):
        render_statement(INSERT_TDNET_ITEM, {"yanoshin_id": 1})


def test_an_id_the_database_assigns_is_rendered_as_a_lookup():
    rendered = render_statement(
        INSERT_TDNET_ITEM,
        {
            key: None
            for key in (
                "yanoshin_id,pubdate,raw_company_code,normalised_company_code,code_normalisation,"
                "company_name,title,document_url,url_xbrl,markets_string,update_history,security_id,"
                "listing_market_code,mapping_confidence,unmapped_reason,source_endpoint,"
                "raw_response_sha256,system_first_seen_at,ingested_at,available_to_model_at"
            ).split(",")
        },
        expressions={"document_id": "(select 1)"},
    )
    assert "(select 1)" in rendered
    assert "%(document_id)s" not in rendered


def _smoke_dir(tmp_path: Path, *, universe: dict | None = None) -> Path:
    raw = _RESPONSE_TEXT.encode("utf-8")
    (tmp_path / "raw_response.json").write_bytes(raw)
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "endpoint": "https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json?limit=3",
                "fetched_at": FETCHED.isoformat(),
                "now": NOW.isoformat(),
                "limit": 3,
                "raw_file": "raw_response.json",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "resolutions.json").write_text(
        json.dumps(
            {
                "resolved": [
                    {
                        "code": "9273",
                        "security_id": "11111111-1111-1111-1111-111111111111",
                        "market_code": "JP",
                    }
                ],
                "universe": universe or {},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_the_rendered_transaction_writes_no_body_text(tmp_path):
    """The whole point of this source's policy, checked on the actual SQL."""

    render(_smoke_dir(tmp_path))
    sql = (tmp_path / "smoke.sql").read_text(encoding="utf-8")

    assert "begin;" in sql and sql.rstrip().endswith("commit;")
    assert "METADATA_ONLY" in sql
    assert "FULL_TEXT" not in sql
    # body_text is a column in the statement; it must never carry a value.
    assert "news.assert_storage_allows" in sql
    body_values = [line for line in sql.splitlines() if "body_text" in line]
    assert body_values, "the document statement should still name the column"


def test_nothing_is_promoted_without_a_universe_read(tmp_path):
    render(_smoke_dir(tmp_path))
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert report["gate"]["promoted"] == 0
    assert report["gate"]["UNRESOLVED"] >= 1
    assert report["bodies_fetched"] == 0
    assert report["bodies_stored"] == 0
    transaction = (tmp_path / "smoke.sql").read_text(encoding="utf-8").split("begin;", 1)[1]
    assert "material.candidates" not in transaction


def test_an_included_security_is_promoted_and_an_excluded_one_is_not(tmp_path):
    universe = {
        "11111111-1111-1111-1111-111111111111": ["INCLUDED", "COMMON_STOCK"],
    }
    render(_smoke_dir(tmp_path, universe=universe))
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert report["gate"]["promoted"] == 1
    assert report["gate"]["INCLUDED"] == 1


def test_the_unresolvable_row_is_still_written(tmp_path):
    """The ETN's code resolves to nothing. It is still a real disclosure, and the
    index row is stored with the reason it did not map - the alternative is a
    coverage number that silently omits what the master has not caught up with."""

    render(_smoke_dir(tmp_path))
    sql = (tmp_path / "smoke.sql").read_text(encoding="utf-8")
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert "587A4" in sql
    assert report["summary"]["unmapped_company_codes"] >= 1
    assert "is not in the security master" in sql


def test_the_two_confidences_in_one_evidence_blob_are_named_apart(tmp_path):
    """``mapping_confidence`` says which security; ``classification_confidence``
    says which kind of disclosure. They sit in adjacent JSON and would otherwise
    both be called "confidence", which reads as a second opinion on the same
    question rather than an answer to a different one."""

    render(_smoke_dir(tmp_path))
    sql = (tmp_path / "smoke.sql").read_text(encoding="utf-8")

    assert '"classification_confidence"' in sql
    assert '"mapping_confidence"' in sql
    assert '"confidence"' not in sql
