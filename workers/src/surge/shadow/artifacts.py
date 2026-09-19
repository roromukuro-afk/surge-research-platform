"""The shadow's artifacts in an object store: write-once objects and the records that commit them (D-279).

Layout, under ``surge/phase-b/<cohort_id>/``::

    cohort.json                      the cohort as created (hashes of the method, never its text)
    calendar.json                    JPX's closed days the jobs use, so a reader needs no copy of its own
    credit/typesafe-<n>.json         the operator's console readings
    stops/cohort-stopped-<n>.json    a stopped cohort
    reports/report-<n>.json          rolling reports; reports/report-final.json once
    runs/<JST date>/<job>-<n>.json   one record per scheduled invocation, whatever it did
    <S0>/...                         one business day (see DAY_FILES)
    <S0>/integrity.json              the day's commit point, written last
    <S0>/outcomes.jsonl              after T+20
    <S0>/outcomes-integrity.json     the outcomes' commit point

Every object is written once (``ObjectStore.put_immutable``): the same bytes
again are a re-run, other bytes are a conflict. A day exists for a reader only
once its integrity record does, and every object it names is read against the
SHA-256 recorded there - an object the record does not vouch for is not read.

JSON is serialized exactly as the PC's ``RunStore`` writes it, so an artifact
that is the same content as a PC file has the same SHA-256 (for a gzip object,
``content_sha256`` is the digest of what it holds). The request bodies - which
carry Canonical v5.1 and the addenda in full - are not stored: their digests are
in ``requests.jsonl``, and they are rebuilt from the stored inputs and the method
at the recorded hashes.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from surge.providers.yahoo_finance import JST
from surge.storage.base import ImmutableObjectConflict, ObjectStore, sha256_hex

SCHEMA = "phase-b-shadow-artifacts-1"
ROOT = "surge/phase-b"
DAY_COMMIT = "integrity.json"
OUTCOME_COMMIT = "outcomes-integrity.json"
JSON = "application/json"
JSONL = "application/x-ndjson"
GZIP = "application/gzip"
#: What a day may hold; the integrity record lists the ones it does.
DAY_FILES = (
    "manifest.json",
    "sessions.json",
    "inputs/universe.json.gz",
    "screening.jsonl.gz",
    "candidates.jsonl.gz",
    "population.jsonl",
    "inputs/prices-selected.jsonl.gz",
    "inputs/price-digests.json",
    "inputs/tdnet.jsonl.gz",
    "requests.jsonl",
    "responses.jsonl.gz",
    "predictions.jsonl",
    "predictions-variants.jsonl",
    "run.json",
    "stopped.json",
)
SYSTEMS = ("vercel-shadow", "pc-official", "synthetic-fixture")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COHORT = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
_JOB = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class IntegrityError(RuntimeError):
    """An artifact is missing, or is not what its commit record says it is."""


def json_bytes(payload) -> bytes:
    """``RunStore.write_json``'s bytes."""

    return (json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n").encode()


def jsonl_bytes(rows: Iterable[dict]) -> bytes:
    """``RunStore.write_jsonl``'s bytes."""

    return "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows).encode()


def gzip_bytes(data: bytes) -> bytes:
    """The same bytes compress to the same object: no timestamp, no file name."""

    return gzip.compress(data, compresslevel=9, mtime=0)


def check_name(name: str) -> str:
    parts = name.split("/")
    if not name or any(not _SEGMENT.match(part) or part in {".", ".."} for part in parts):
        raise ValueError(f"not an artifact name: {name!r}")
    return name


def cohort_prefix(cohort_id: str, *, root: str = ROOT) -> str:
    if not _COHORT.match(cohort_id or ""):
        raise ValueError(f"{cohort_id!r}: a cohort id is 3-63 lowercase letters, digits and hyphens")
    return f"{root.strip('/')}/{cohort_id}"


def day_prefix(cohort_id: str, s0: date, *, root: str = ROOT) -> str:
    return f"{cohort_prefix(cohort_id, root=root)}/{s0.isoformat()}"


def run_record_name(job: str, started_at: datetime, n: int) -> str:
    """Where the n-th record of ``job`` started on a given day (JST) lives, relative to the cohort."""

    if not _JOB.match(job) or n < 1:
        raise ValueError(f"not a run record: {job!r} #{n}")
    return f"runs/{started_at.astimezone(JST).date().isoformat()}/{job}-{n}.json"


@dataclass(frozen=True)
class Artifact:
    name: str
    key: str
    sha256: str
    bytes: int
    content_type: str
    #: For a gzip object: the SHA-256 of what it holds (comparable with the PC's hash of the same file).
    content_sha256: str | None = None

    def entry(self) -> dict:
        entry = {"sha256": self.sha256, "bytes": self.bytes, "content_type": self.content_type}
        if self.content_sha256 is not None:
            entry["content_sha256"] = self.content_sha256
        return entry


def exists(store: ObjectStore, key: str) -> bool:
    """Whether a key is taken, as cheaply as the store allows (Blob: a head, not a download)."""

    probe = getattr(store, "exists", None)
    return bool(probe(key)) if callable(probe) else store.head(key) is not None


class ArtifactSet:
    """The artifacts under one prefix, each written once, and the record that commits them."""

    def __init__(self, store: ObjectStore, prefix: str) -> None:
        self.store = store
        self.prefix = prefix.strip("/")
        self.artifacts: dict[str, Artifact] = {}

    def key(self, name: str) -> str:
        return f"{self.prefix}/{check_name(name)}"

    def put(self, name: str, data: bytes, content_type: str, *, content_sha256: str | None = None) -> Artifact:
        if name in self.artifacts:
            raise ImmutableObjectConflict(f"{name} is already part of {self.prefix}")
        stored = self.store.put_immutable(self.key(name), data, content_type)
        artifact = Artifact(name, stored.key, stored.sha256, stored.bytes, content_type, content_sha256)
        self.artifacts[name] = artifact
        return artifact

    def put_json(self, name: str, payload) -> Artifact:
        return self.put(name, json_bytes(payload), JSON)

    def put_jsonl(self, name: str, rows: Iterable[dict]) -> Artifact:
        return self.put(name, jsonl_bytes(rows), JSONL)

    def put_gzip(self, name: str, content: bytes) -> Artifact:
        if not name.endswith(".gz"):
            raise ValueError(f"{name}: a gzip artifact's name ends in .gz")
        return self.put(name, gzip_bytes(content), GZIP, content_sha256=sha256_hex(content))

    def commit(self, name: str, record: dict, *, committed_at: str) -> Artifact:
        """Write the record naming every artifact put so far; it is the set's commit point, written once."""

        if name in self.artifacts:
            raise ValueError(f"{name} is the commit record; it cannot also be an artifact")
        payload = {"schema": SCHEMA, "prefix": self.prefix, "committed_at": committed_at, **record,
                   "artifacts": {n: a.entry() for n, a in sorted(self.artifacts.items())}}
        # Not an artifact of the set: a second, different commit of the same set is a conflict, not a member.
        stored = self.store.put_immutable(self.key(name), json_bytes(payload), JSON)
        return Artifact(name, stored.key, stored.sha256, stored.bytes, JSON)


def read_commit(store: ObjectStore, prefix: str, name: str) -> dict | None:
    """A commit record, or None while the set is not committed."""

    key = f"{prefix.strip('/')}/{check_name(name)}"
    if not exists(store, key):
        return None
    record = json.loads(store.get(key).decode("utf-8"))
    if record.get("schema") != SCHEMA:
        raise IntegrityError(f"{key}: schema {record.get('schema')!r}, expected {SCHEMA!r}")
    return record


def read_verified(store: ObjectStore, prefix: str, commit: dict, name: str) -> bytes:
    """An artifact's bytes, checked against its commit record; gzip objects come back decompressed."""

    entry = commit.get("artifacts", {}).get(name)
    if entry is None:
        raise IntegrityError(f"{prefix}/{name} is not in its commit record")
    data = store.get(f"{prefix.strip('/')}/{check_name(name)}")
    if sha256_hex(data) != entry["sha256"] or len(data) != entry["bytes"]:
        raise IntegrityError(f"{prefix}/{name} does not match its commit record")
    if entry.get("content_type") == GZIP:
        content = gzip.decompress(data)
        if sha256_hex(content) != entry.get("content_sha256"):
            raise IntegrityError(f"{prefix}/{name}: the content does not match its commit record")
        return content
    return data


def verify_committed(store: ObjectStore, prefix: str, commit: dict) -> list[str]:
    """Every artifact the record names, read and checked; the problems, or an empty list."""

    problems = []
    for name in sorted(commit.get("artifacts", {})):
        try:
            read_verified(store, prefix, commit, name)
        except (IntegrityError, OSError, RuntimeError) as exc:
            problems.append(f"{name}: {exc}")
    return problems


def put_run_record(store: ObjectStore, cohort_id: str, job: str, record: dict, *, started_at: datetime,
                   root: str = ROOT, max_per_day: int = 50) -> str:
    """Record one scheduled invocation under the first free ``runs/<date>/<job>-<n>.json``; returns the key."""

    base = cohort_prefix(cohort_id, root=root)
    data = json_bytes(record)
    for n in range(1, max_per_day + 1):
        key = f"{base}/{run_record_name(job, started_at, n)}"
        try:
            # No probe first: on Blob a head is an operation too, and the first number is nearly always free.
            # The same record again lands on its own key (a re-run); another record's number is taken.
            store.put_immutable(key, data, JSON)
        except ImmutableObjectConflict:
            continue
        return key
    raise IntegrityError(f"more than {max_per_day} {job} records on {started_at.astimezone(JST).date()}")


__all__ = ["DAY_COMMIT", "DAY_FILES", "GZIP", "JSON", "JSONL", "OUTCOME_COMMIT", "ROOT", "SCHEMA", "SYSTEMS",
           "Artifact", "ArtifactSet", "IntegrityError", "check_name", "cohort_prefix", "day_prefix", "exists",
           "gzip_bytes", "json_bytes", "jsonl_bytes", "put_run_record", "read_commit", "read_verified",
           "run_record_name", "verify_committed"]
