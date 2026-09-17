"""A one-time, metadata-only smoke pass against the cloud schema.

This exists because the production writers in ``surge.news.db`` and
``surge.material.db`` need a psycopg2 connection and the cloud database is only
reachable here through a SQL-execution channel. Rather than hand-write a second
set of inserts - which is exactly how a column name or a cast quietly diverges
from the migration - this module **renders the production statements verbatim**
and substitutes literals for their parameters.

So there is one definition of which columns are written and with which casts:
``INSERT_DOCUMENT``, ``INSERT_TDNET_ITEM``, ``INSERT_EVENT`` and the rest. If a
migration changes a column, the statement changes, and this renderer changes
with it because it has no column list of its own.

Two parameters are rendered as subqueries rather than as literals, because their
values are assigned by the database: ``document_id`` becomes a lookup on the
document's natural key, and ``event_id`` a lookup on ``(event_key,
merge_version)``. Everything else is a literal.

What this pass must not do, and does not:

* fetch a TDnet PDF or XBRL file - nothing here follows ``document_url``;
* write any body text - every document is ``METADATA_ONLY`` and the
  ``news.tdnet_items`` table has no body column at all;
* promote anything to a formal candidate - the universe gate's verdicts are
  reported, and ``material.candidates`` is not written by this pass.

Run in two steps so the live fetch happens exactly once::

    python -m surge.jobs.tdnet_smoke plan  --out DIR --limit 12
    # execute DIR/resolve.sql, save the rows as DIR/resolutions.json
    python -m surge.jobs.tdnet_smoke render --dir DIR

The raw response is kept in ``DIR`` and never committed: it is third-party data
this repository is not licensed to redistribute.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from surge.jobs.tdnet_discovery import JOB_VERSION, DatabaseResolver, TdnetDiscoveryJob
from surge.material.db import (
    INSERT_EVENT,
    INSERT_EVENT_SOURCE,
    INSERT_FEATURES,
    INSERT_RELATION,
    event_params,
    event_source_params,
    feature_params,
    relation_params,
)
from surge.material.from_tdnet import to_event, to_features, to_relation
from surge.material.universe_gate import UNIVERSE_GATE_VERSION, apply_gate
from surge.news.db import (
    INSERT_DOCUMENT,
    INSERT_TDNET_COVERAGE,
    INSERT_TDNET_ITEM,
    document_params,
)
from surge.news.sources.yanoshin_tdnet import SOURCE_KEY, YanoshinTdnetSource, parse_response

SMOKE_VERSION = "tdnet-cloud-smoke-1.0.0"

#: The document row this smoke writes is looked up by its natural key, which is
#: the same key the insert conflicts on.
DOCUMENT_ID_SUBQUERY = (
    "(select d.document_id from news.documents d "
    "where d.source_key = {source_key} and d.source_document_id = {source_document_id} "
    "and d.content_sha256 = {content_sha256})"
)

EVENT_ID_SUBQUERY = (
    "(select e.event_id from material.events e "
    "where e.event_key = {event_key} and e.merge_version = {merge_version})"
)

_PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def literal(value) -> str:
    """One value as a SQL literal.

    Deliberately narrow. Anything not in this list raises rather than being
    coerced, because a silent ``str()`` of an unexpected type is how a Decimal
    becomes a float or a naive datetime loses its zone.
    """

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal | float):
        return repr(float(value))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive datetime would be read in the server's zone: {value!r}")
        return "'" + value.astimezone(UTC).isoformat() + "'::timestamptz"
    if isinstance(value, date):
        return "'" + value.isoformat() + "'::date"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"no literal form for {type(value).__name__}: {value!r}")


def render_statement(sql: str, params: dict, *, expressions: dict[str, str] | None = None) -> str:
    """Substitute a production statement's parameters with literals.

    ``expressions`` replaces a parameter with raw SQL instead of a literal, for
    the two ids the database assigns.
    """

    expressions = expressions or {}
    stripped = _PLACEHOLDER.sub("", sql)
    if "%" in stripped:
        raise ValueError("the statement contains a bare % that is not a placeholder")

    missing = sorted(set(_PLACEHOLDER.findall(sql)) - set(params) - set(expressions))
    if missing:
        raise KeyError(f"no value for {missing}")

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in expressions:
            return expressions[name]
        return literal(params[name])

    return _PLACEHOLDER.sub(replace, sql).strip()


@dataclass
class SmokePlan:
    """What the live fetch produced, saved so the render step is deterministic."""

    endpoint: str
    fetched_at: datetime
    now: datetime
    limit: int
    raw_path: Path

    def to_json(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "fetched_at": self.fetched_at.isoformat(),
            "now": self.now.isoformat(),
            "limit": self.limit,
            "raw_file": self.raw_path.name,
            "smoke_version": SMOKE_VERSION,
            "job_version": JOB_VERSION,
        }


class StaticResolver:
    """The bitemporal resolver's answers, obtained once and replayed.

    The query that produced them is ``DatabaseResolver.SQL`` rendered against the
    same code, effective time and knowledge time this pass uses - not a
    convenience lookup on current rows.
    """

    def __init__(self, answers: dict[str, tuple[str | None, str | None]]) -> None:
        self._answers = answers

    def resolve(self, normalised_code: str, *, as_of, known_at=None):
        return self._answers.get(normalised_code, (None, None))

    def resolve_code(self, code, *, as_of, known_at=None):
        for key in code.lookup_keys:
            security_id, market_code = self.resolve(key, as_of=as_of, known_at=known_at)
            if security_id is not None:
                return security_id, market_code, key
        return None, None, None


def plan(out_dir: Path, *, limit: int) -> SmokePlan:
    """Fetch once, save the response, and write the resolution query."""

    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    source = YanoshinTdnetSource(limit=limit)
    payload, endpoint, fetched_at = _fetch_raw(source, now=now)

    raw_path = out_dir / "raw_response.json"
    raw_path.write_bytes(payload)

    result = parse_response(payload, endpoint=endpoint, fetched_at=fetched_at)

    asked: list[tuple[str, datetime, datetime]] = []
    seen: set[tuple[str, str]] = set()
    for item in result.items:
        # available_to_model_at is the knowledge time: it is derived from when we
        # ingested, never from when the issuer published.
        known_at = now if now >= fetched_at else fetched_at
        for key in item.code.lookup_keys:
            fingerprint = (key, item.pubdate.isoformat())
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            asked.append((key, item.pubdate, known_at))

    (out_dir / "resolve.sql").write_text(_resolve_sql(asked), encoding="utf-8")

    saved = SmokePlan(
        endpoint=endpoint, fetched_at=fetched_at, now=now, limit=limit, raw_path=raw_path
    )
    (out_dir / "plan.json").write_text(
        json.dumps(saved.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return saved


def _fetch_raw(source: YanoshinTdnetSource, *, now: datetime) -> tuple[bytes, str, datetime]:
    """The live call, kept separate so the raw bytes can be saved.

    ``fetch_recent`` parses and discards them; the smoke needs the bytes because
    the response hash the rows are stored under must be the hash of what actually
    came back.
    """

    from surge.http_fetch import fetch as http_fetch
    from surge.news.sources.yanoshin_tdnet import BASE_URL

    endpoint = f"{BASE_URL}/recent.json?limit={source.limit}"
    response = http_fetch(
        endpoint,
        user_agent=source.user_agent,
        timeout=source.timeout,
        min_interval=source.pause_seconds,
    )
    if response.status != 200:
        raise RuntimeError(f"{SOURCE_KEY}: HTTP {response.status} from {endpoint}")
    return response.body, endpoint, now


def _resolve_sql(asked: list[tuple[str, datetime, datetime]]) -> str:
    """The production resolver query, run once for every code we saw.

    Rendered from ``DatabaseResolver.SQL`` rather than rewritten, so the join,
    the market filter and the ordering are the ones production uses.
    """

    if not asked:
        return "-- nothing to resolve\n"

    rows = ",\n  ".join(
        f"({literal(code)}, {literal(effective_at)}, {literal(known_at)})"
        for code, effective_at, known_at in asked
    )
    inner = render_statement(
        DatabaseResolver.SQL,
        {},
        expressions={
            "code": "a.code",
            "effective_at": "a.effective_at",
            "known_at": "a.known_at",
        },
    )
    return (
        "with asked (code, effective_at, known_at) as (values\n  "
        + rows
        + "\n)\nselect a.code, r.security_id, r.market_code\nfrom asked a\n"
        "left join lateral (\n"
        + "\n".join("  " + line for line in inner.splitlines())
        + "\n) r on true;\n"
    )


def render(dir_path: Path) -> str:
    """Build the transaction from the saved response and the saved answers."""

    saved = json.loads((dir_path / "plan.json").read_text(encoding="utf-8"))
    payload = (dir_path / saved["raw_file"]).read_bytes()
    now = datetime.fromisoformat(saved["now"])
    fetched_at = datetime.fromisoformat(saved["fetched_at"])
    endpoint = saved["endpoint"]

    resolutions = json.loads((dir_path / "resolutions.json").read_text(encoding="utf-8"))
    answers = {
        row["code"]: (row.get("security_id"), row.get("market_code"))
        for row in resolutions.get("resolved", [])
        if row.get("security_id")
    }
    universe = {
        str(security_id): (decision, reason)
        for security_id, (decision, reason) in (resolutions.get("universe") or {}).items()
    }

    result = parse_response(payload, endpoint=endpoint, fetched_at=fetched_at)
    response_sha256 = _sha256(payload)

    job = TdnetDiscoveryJob(source=_ReplaySource(result), resolver=StaticResolver(answers))
    report = job.run(now=now)

    verdicts = apply_gate(
        sorted({i.security_id for i in report.items if i.security_id}), universe or None
    )

    statements = _statements(report, response_sha256=response_sha256, endpoint=endpoint, now=now)
    header = _header(report, verdicts, universe_supplied=bool(universe))
    body = "\n\n".join(statements)
    (dir_path / "smoke.sql").write_text(
        f"{header}begin;\n\n{body}\n\ncommit;\n", encoding="utf-8"
    )
    (dir_path / "report.json").write_text(
        json.dumps(
            {
                "summary": report.summary,
                "notes": report.notes,
                "gate": verdicts.summary,
                "gate_version": UNIVERSE_GATE_VERSION,
                "bodies_fetched": 0,
                "bodies_stored": 0,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return str(dir_path / "smoke.sql")


def _header(report, verdicts, *, universe_supplied: bool) -> str:
    lines = [
        f"-- {SMOKE_VERSION}: one metadata-only pass over {report.items_retrieved} index rows.",
        "-- No TDnet PDF or XBRL file was fetched and no body text is written:",
        "-- every document is METADATA_ONLY and news.tdnet_items has no body column.",
        f"-- universe gate {UNIVERSE_GATE_VERSION}: {verdicts.summary}",
    ]
    if not universe_supplied:
        lines.append(
            "-- no universe read was supplied, so every security is UNRESOLVED and nothing "
            "would be promoted to a formal candidate. material.candidates is not written here."
        )
    return "\n".join(lines) + "\n\n"


def _statements(report, *, response_sha256: str, endpoint: str, now: datetime) -> list[str]:
    statements: list[str] = []
    for discovered in report.items:
        document = discovered.document
        doc_params = document_params(document)
        document_id = DOCUMENT_ID_SUBQUERY.format(
            source_key=literal(document.source_key),
            source_document_id=literal(document.source_document_id),
            content_sha256=literal(document.content_sha256),
        )

        statements.append(
            "select news.assert_storage_allows("
            f"{literal(document.source_key)}, "
            f"{literal(document.body_storage.value)}::news.body_storage);"
        )
        statements.append(render_statement(INSERT_DOCUMENT, doc_params) + ";")

        tdnet = discovered.tdnet_row(
            run_id=None, response_sha256=response_sha256, endpoint=endpoint
        )
        statements.append(
            render_statement(
                INSERT_TDNET_ITEM,
                tdnet,
                expressions={"document_id": document_id},
            )
            + ";"
        )

        event = to_event(discovered)
        statements.append(render_statement(INSERT_EVENT, event_params(event)) + ";")
        event_id = EVENT_ID_SUBQUERY.format(
            event_key=literal(event.event_key), merge_version=literal(event.merge_version)
        )

        for source in event.sources:
            statements.append(
                render_statement(
                    INSERT_EVENT_SOURCE,
                    event_source_params(source, event_id=""),
                    expressions={"event_id": event_id, "document_id": document_id},
                )
                + ";"
            )

        relation = to_relation(discovered)
        if relation is not None:
            statements.append(
                render_statement(
                    INSERT_RELATION,
                    relation_params(relation, event_id=""),
                    expressions={"event_id": event_id},
                )
                + ";"
            )

        features = to_features(discovered, knowledge_cutoff=now)
        if features is not None:
            statements.append(
                render_statement(
                    INSERT_FEATURES,
                    feature_params(features, event_id=""),
                    expressions={"event_id": event_id},
                )
                + ";"
            )

    coverage = report.as_coverage_row()
    coverage["notes"] = "\n".join(
        [*report.notes, f"{SMOKE_VERSION}: metadata only, 0 bodies fetched, 0 bodies stored"]
    )
    statements.append(render_statement(INSERT_TDNET_COVERAGE, coverage) + ";")
    return statements


class _ReplaySource:
    """Hands the discovery job the response we already fetched."""

    def __init__(self, result) -> None:
        self._result = result

    def fetch_recent(self, *, now=None):
        return self._result

    def window_is_safe(self, result) -> bool:
        return True

    def advance_cursor(self, result):
        return result.max_id


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    planner = sub.add_parser("plan", help="fetch once and write the resolution query")
    planner.add_argument("--out", required=True, type=Path)
    planner.add_argument("--limit", type=int, default=12)

    renderer = sub.add_parser("render", help="build the transaction from the saved response")
    renderer.add_argument("--dir", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "plan":
        saved = plan(args.out, limit=args.limit)
        print(json.dumps(saved.to_json(), ensure_ascii=False, indent=2))
    else:
        print(render(args.dir))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SMOKE_VERSION",
    "StaticResolver",
    "literal",
    "plan",
    "render",
    "render_statement",
]
