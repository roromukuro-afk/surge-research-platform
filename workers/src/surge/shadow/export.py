"""A Phase B cohort the PC wrote, in the shadow's artifact format (D-279).

The PC's ``RunStore`` layout (D-277, D-278) keeps one file per request, per
answer and per disclosure index, parquet copies and stage records; the shadow
keeps a handful of objects per day (``surge.shadow.artifacts``) because every
object written to Vercel Blob is a billed operation. This module maps one onto
the other without changing a row:

- files whose content is kept whole (manifest, sessions, universe, candidates,
  population, requests, predictions, outcomes) are copied byte for byte, so
  their SHA-256 - or, gzipped, their ``content_sha256`` - is the PC's;
- per-file sets (disclosure indexes, answers) become one JSON-lines object whose
  rows carry each PC file's name and SHA-256;
- the price archive is reduced to the selected securities' lines, and every
  line's SHA-256 is kept, so the whole archive stays checkable line by line;
- request bodies are not copied: they hold Canonical v5.1 and the addenda in
  full, their digests are already in ``requests.jsonl``, and they are rebuilt
  from the stored inputs and the method at the recorded hashes;
- records that describe the machine (a path, a process id, a traceback, a host
  name) are scrubbed; a byte-for-byte file that names a local path is refused.

It reads the PC's files and writes only to the target store. A day is exported
once it is final - sent, or stopped - and only once: a committed day is left
alone.

    python -m surge.shadow.export --cohort-id C --target local:<dir> [--system pc-official]

``--target vercel-blob`` writes to the shadow's private store (BLOB_READ_WRITE_TOKEN
from the environment); every object is an advanced operation of the team's
monthly Blob allowance, so the count is printed.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

from surge.evaluation import jpx_calendar, phase_b
from surge.evaluation.report import prediction_row
from surge.evaluation.store import RunStore, default_root
from surge.shadow.artifacts import (
    DAY_COMMIT,
    JSON,
    JSONL,
    OUTCOME_COMMIT,
    ROOT,
    SYSTEMS,
    ArtifactSet,
    cohort_prefix,
    day_prefix,
    json_bytes,
    jsonl_bytes,
    put_run_record,
    read_commit,
)
from surge.storage.base import ObjectStore, sha256_hex

#: An absolute path on the machine that wrote a record: a drive, a UNC share, or a home or temp directory.
_LOCAL_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|/(?:home|Users|tmp|var|private|mnt|root)/)"
                         r"[^\s'\"<>|,;)]*")
_MACHINE_KEYS = {"pid", "host", "traceback"}
REQUEST_BODIES = ("not stored: each body's SHA-256 is in requests.jsonl, and a body is rebuilt from the stored "
                  "inputs and the method at the recorded hashes (it holds Canonical v5.1 and the addenda in full)")


class ExportError(RuntimeError):
    pass


def scrub(value):
    """A record without what names the machine it was written on."""

    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in _MACHINE_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _LOCAL_PATH.sub("<local path>", value)
    return value


def _verbatim(store: RunStore, name: str) -> bytes:
    """A file copied byte for byte; one that names a local path is refused, not quietly edited."""

    data = (store.path / name).read_bytes()
    if _LOCAL_PATH.search(data.decode("utf-8", errors="replace")):
        raise ExportError(f"{store.run_id}/{name} names a local path; it is not exported")
    return data


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _numbered(store: RunStore, stem: str) -> list[dict]:
    rows, n = [], 1
    while store.exists(f"{stem}-{n}.json"):
        rows.append(scrub(store.read_json(f"{stem}-{n}.json")))
        n += 1
    return rows


def _parquet_rows(path: Path) -> list[dict]:
    """The screening rows the PC keeps as parquet, with their list columns decoded again."""

    import pyarrow.parquet as pq  # noqa: PLC0415 - only exports read parquet

    rows = pq.read_table(path).to_pylist()
    for row in rows:
        if isinstance(row.get("routes"), str):
            row["routes"] = json.loads(row["routes"])
    return rows


def _price_lines(archive: bytes) -> list[tuple[str, bytes]]:
    lines = []
    with gzip.open(io.BytesIO(archive), "rb") as handle:
        for line in handle:
            if line.strip():
                lines.append((json.loads(line)["code"], line))
    return lines


def _file_rows(store: RunStore, folder: str) -> list[dict]:
    root = store.path / folder
    if not root.exists():
        return []
    return [{"file": f"{folder}/{path.name}", "sha256": sha256_hex(path.read_bytes()), "record": scrub(_json(path))}
            for path in sorted(root.glob("*.json"))]


def _variant_rows(store: RunStore) -> list[dict]:
    """The anonymized and drift answers as prediction rows, by the same frozen ``prediction_row`` as the main ones.

    The PC keeps only the main rows as JSON lines (the others as pairs, in
    parquet); a reader of the shadow sees every request's answer.
    """

    if not (store.exists("requests.jsonl") and store.exists("population.jsonl") and store.exists("manifest.json")):
        return []
    manifest = store.read_json("manifest.json")
    samples = {r["sample_id"]: r for r in store.read_jsonl("population.jsonl")}
    rows = []
    for request in store.read_jsonl("requests.jsonl"):
        name = f"responses/{request['sample_id']}.{request['variant']}.json"
        if request["variant"] == "main" or not store.exists(name):
            continue
        rows.append(prediction_row(store.read_json(name), samples[request["sample_id"]], variant=request["variant"],
                                   request=request, evaluation_version=manifest["evaluation_version"],
                                   question_schema_hash=manifest["question_schema_hash"]))
    return rows


def _status(store: RunStore) -> str | None:
    if store.exists("stage-run.json"):
        return "sent"
    if store.exists("run-stopped-1.json") or store.exists("day-stopped-1.json"):
        return "stopped"
    return None  # still in progress: not exported until it is final


def export_day(store: RunStore, target: ObjectStore, cohort_id: str, s0: date, *, system: str,
               exported_at: str, root: str = ROOT) -> dict:
    """One final day's artifacts and its integrity record; a day already committed is left as it is."""

    prefix = day_prefix(cohort_id, s0, root=root)
    status = _status(store)
    if status is None:
        return {"s0": s0.isoformat(), "exported": False, "reason": "not final (neither sent nor stopped)"}
    if read_commit(target, prefix, DAY_COMMIT) is not None:
        return {"s0": s0.isoformat(), "exported": False, "reason": "already committed"}
    parts = ArtifactSet(target, prefix)
    for name in ("manifest.json", "sessions.json"):
        if store.exists(name):
            parts.put(name, _verbatim(store, name), JSON)
    if store.exists("inputs/universe.json"):
        parts.put_gzip("inputs/universe.json.gz", _verbatim(store, "inputs/universe.json"))
    if store.exists("screening.parquet"):
        parts.put_gzip("screening.jsonl.gz", jsonl_bytes(_parquet_rows(store.path / "screening.parquet")))
    if store.exists("candidates.jsonl"):
        parts.put_gzip("candidates.jsonl.gz", _verbatim(store, "candidates.jsonl"))
    population = store.read_jsonl("population.jsonl") if store.exists("population.jsonl") else []
    if store.exists("population.jsonl"):
        parts.put("population.jsonl", _verbatim(store, "population.jsonl"), JSONL)
    if store.exists("inputs/prices.jsonl.gz"):
        archive = (store.path / "inputs" / "prices.jsonl.gz").read_bytes()
        lines = _price_lines(archive)
        selected = {row["code"] for row in population}
        parts.put_gzip("inputs/prices-selected.jsonl.gz", b"".join(line for code, line in lines if code in selected))
        parts.put_json("inputs/price-digests.json", {
            "rule": "the SHA-256 of each security's line in the day's price archive, as written; the selected "
                    "securities' lines are in inputs/prices-selected.jsonl.gz",
            "archive_sha256": sha256_hex(archive),
            "lines": {code: sha256_hex(line) for code, line in lines},
        })
    tdnet = _file_rows(store, "inputs/tdnet")
    if tdnet:
        parts.put_gzip("inputs/tdnet.jsonl.gz", jsonl_bytes(tdnet))
    if store.exists("requests.jsonl"):
        parts.put("requests.jsonl", _verbatim(store, "requests.jsonl"), JSONL)
    responses = _file_rows(store, "responses")
    if responses:
        parts.put_gzip("responses.jsonl.gz", jsonl_bytes(responses))
    if store.exists("predictions.jsonl"):
        parts.put("predictions.jsonl", _verbatim(store, "predictions.jsonl"), JSONL)
        variants = _variant_rows(store)
        if variants:
            parts.put_jsonl("predictions-variants.jsonl", variants)
    stages = {path.stem.removeprefix("stage-"): scrub(_json(path))
              for path in sorted(store.path.glob("stage-*.json")) if path.stem != "stage-outcomes"}
    parts.put_json("run.json", {
        "stages": stages,
        "preflight": _numbered(store, "preflight"),
        "run_started": _numbered(store, "run-started"),
        "run_stopped": _numbered(store, "run-stopped"),
        "day_stopped": _numbered(store, "day-stopped"),
    })
    pc_hashes = {name: sha for stage in stages.values() for name, sha in (stage.get("files_sha256") or {}).items()}
    commit = parts.commit(DAY_COMMIT, {
        "system": system,
        "official": system == "pc-official",
        "cohort_id": cohort_id,
        "s0": s0.isoformat(),
        "status": status,
        "source": {"layout": "RunStore (the PC, D-277)", "files_sha256": pc_hashes},
        "request_bodies": REQUEST_BODIES,
        "teacher_admissible": False,
        "database_writes": "none",
    }, committed_at=exported_at)
    return {"s0": s0.isoformat(), "exported": True, "status": status, "artifacts": len(parts.artifacts),
            "integrity_sha256": commit.sha256}


def export_outcomes(store: RunStore, target: ObjectStore, cohort_id: str, s0: date, *, system: str,
                    exported_at: str, root: str = ROOT) -> dict:
    """The day's frozen outcomes and their commit record, once they exist."""

    prefix = day_prefix(cohort_id, s0, root=root)
    if not store.exists("stage-outcomes.json"):
        return {"s0": s0.isoformat(), "exported": False, "reason": "no frozen outcomes"}
    if read_commit(target, prefix, OUTCOME_COMMIT) is not None:
        return {"s0": s0.isoformat(), "exported": False, "reason": "already committed"}
    stage = store.read_json("stage-outcomes.json")
    parts = ArtifactSet(target, prefix)
    parts.put("outcomes.jsonl", _verbatim(store, stage["outcomes_file"]), JSONL)
    parts.commit(OUTCOME_COMMIT, {"system": system, "official": system == "pc-official", "cohort_id": cohort_id,
                                  "s0": s0.isoformat(), "stage": scrub(stage), "teacher_admissible": False},
                 committed_at=exported_at)
    return {"s0": s0.isoformat(), "exported": True, "t_plus_20": stage.get("t_plus_20")}


def calendar_record() -> dict:
    """The JPX closed days the jobs decide business days by, for readers that must agree with them."""

    years = sorted({day.year for day in jpx_calendar.CLOSED_DAYS})
    return {"version": jpx_calendar.CALENDAR_VERSION, "source": jpx_calendar.SOURCE_URL,
            "source_sha256": jpx_calendar.SOURCE_SHA256, "years": years,
            "closed_days": {day.isoformat(): reason for day, reason in sorted(jpx_calendar.CLOSED_DAYS.items())},
            "rule": "a business day is a weekday that is not a closed day; a date in a year the list does not "
                    "cover is unknown, never guessed"}


def export_cohort(root: Path, cohort_id: str, target: ObjectStore, *, system: str,
                  exported_at: datetime | None = None, prefix_root: str = ROOT) -> dict:
    """Everything final in a PC cohort: the cohort, the calendar, credit readings, stops, days, outcomes,
    reports and the scheduler's records. Safe to run again: what is committed is not written twice."""

    if system not in SYSTEMS:
        raise ExportError(f"unknown system {system!r}; one of {SYSTEMS}")
    stamp = (exported_at or datetime.now(UTC)).isoformat()
    cohort = phase_b.cohort_store(root, cohort_id)
    if not cohort.exists("cohort.json"):
        raise ExportError(f"no Phase B cohort {cohort_id!r} under the evaluation root")
    base = cohort_prefix(cohort_id, root=prefix_root)
    written: dict = {"cohort_id": cohort_id, "system": system, "days": [], "outcomes": [], "reports": [],
                     "runs": [], "credit": [], "stops": []}

    def put(name: str, data: bytes, content_type: str = JSON) -> None:
        target.put_immutable(f"{base}/{name}", data, content_type)

    put("cohort.json", _verbatim(cohort, "cohort.json"))
    put("calendar.json", json_bytes(calendar_record()))
    credit_dir = root / "evaluation" / "jev" / "credit"
    for path in sorted(credit_dir.glob("typesafe-*.json")) if credit_dir.exists() else []:
        put(f"credit/{path.name}", json_bytes(scrub(_json(path))))
        written["credit"].append(path.name)
    for n, stop in enumerate(phase_b.cohort_stops(root, cohort_id), start=1):
        put(f"stops/cohort-stopped-{n}.json", json_bytes(scrub(stop)))
        written["stops"].append(n)
    for day in phase_b.day_dates(root, cohort_id):
        store = phase_b.day_store(root, cohort_id, day)
        written["days"].append(export_day(store, target, cohort_id, day, system=system, exported_at=stamp,
                                          root=prefix_root))
        written["outcomes"].append(export_outcomes(store, target, cohort_id, day, system=system, exported_at=stamp,
                                                   root=prefix_root))
    for path in sorted(cohort.path.glob("report-*.json")):
        put(f"reports/{path.name}", json_bytes(scrub(_json(path))))
        written["reports"].append(path.name)
    runs_dir = cohort.path / "scheduler" / "runs"
    for path in sorted(runs_dir.glob("*.json")) if runs_dir.exists() else []:
        record = _json(path)
        code = record.get("code") or {}
        record["code"] = {"head": code.get("head"), "expected_commit": code.get("expected_commit")}
        started = datetime.fromisoformat(record["started_at"])
        key = put_run_record(target, cohort_id, record["job"], {**scrub(record), "system": system},
                             started_at=started, root=prefix_root)
        written["runs"].append(key.removeprefix(base + "/"))
    return written


def main(argv: list[str] | None = None) -> int:
    from surge.storage import open_store  # noqa: PLC0415 - the CLI's store, not the library's concern

    parser = argparse.ArgumentParser(description="Export a Phase B cohort the PC wrote into the shadow's artifacts")
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--root", type=Path, help="the evaluation root (default: SURGE_EVALUATION_ROOT or ~/.surge)")
    parser.add_argument("--target", required=True, help="local:<dir>, or vercel-blob for the shadow's private store")
    parser.add_argument("--system", default="pc-official", choices=SYSTEMS)
    args = parser.parse_args(argv)
    store = open_store(args.target)
    written = export_cohort(args.root or default_root(), args.cohort_id, store, system=args.system)
    summary = {
        "cohort_id": written["cohort_id"], "system": written["system"],
        "days": {d["s0"]: d.get("status") if d["exported"] else d["reason"] for d in written["days"]},
        "outcomes": [o["s0"] for o in written["outcomes"] if o["exported"]],
        "reports": written["reports"], "runs": len(written["runs"]),
    }
    operations = getattr(store, "operations", None)
    if operations is not None:
        summary["blob_operations"] = operations.as_dict()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["REQUEST_BODIES", "ExportError", "calendar_record", "export_cohort", "export_day", "export_outcomes",
           "main", "scrub"]
