"""The Phase B web shadow's storage (D-279): Vercel Blob as a write-once store, the artifact format, the export.

Offline: Blob is a fake with Blob's semantics (a PUT without overwrite refuses a
taken key; a private read needs the token), and the cohorts are synthetic,
written by the Phase B code through the fakes of test_evaluation_phase_b.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_evaluation_phase_b as t
from surge.evaluation import phase_b
from surge.evaluation.method import REPO_ROOT
from surge.evaluation.store import RunStore
from surge.providers.yahoo_finance import JST
from surge.shadow import artifacts
from surge.shadow.artifacts import (
    DAY_COMMIT,
    OUTCOME_COMMIT,
    ArtifactSet,
    IntegrityError,
    json_bytes,
    jsonl_bytes,
    put_run_record,
    read_commit,
    read_verified,
    verify_committed,
)
from surge.shadow.export import ExportError, export_cohort, scrub
from surge.storage import open_store
from surge.storage.base import ImmutableObjectConflict, ObjectStoreError, sha256_hex
from surge.storage.local import LocalObjectStore
from surge.storage.vercel_blob import VercelBlobObjectStore

FIXTURE = REPO_ROOT / "apps" / "web" / "fixtures" / "shadow"
FIXTURE_COHORT = "synthetic-phase-b-fixture"


# ----------------------------------------------------------------- a Blob store with Blob's semantics


class BlobError(Exception):
    """Named as the SDK names its base class: the store recognises the SDK's errors by it."""


class BlobNotFoundError(BlobError):
    pass


class FakeBlobClient:
    def __init__(self, *, land_then_refuse: bool = False):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.land_then_refuse = land_then_refuse

    def put(self, path, body, *, access, content_type, add_random_suffix, overwrite):
        self.calls.append(("put", path))
        assert access == "private" and add_random_suffix is False and overwrite is False
        if path in self.objects:
            raise BlobError("This blob already exists, use `allowOverwrite: true` if you want to overwrite it.")
        self.objects[path] = bytes(body)
        if self.land_then_refuse:
            # The PUT landed, its answer was lost, and the SDK's retry was refused: the key is taken - by us.
            raise BlobError("This blob already exists")
        return SimpleNamespace(pathname=path)

    def get(self, path, *, access, use_cache=True):
        self.calls.append(("get", path))
        assert access == "private"
        if path not in self.objects:
            raise BlobNotFoundError("not found")
        return SimpleNamespace(content=self.objects[path])

    def head(self, path):
        self.calls.append(("head", path))
        if path not in self.objects:
            raise BlobNotFoundError("not found")
        return SimpleNamespace(pathname=path, size=len(self.objects[path]))

    def iter_objects(self, *, prefix):
        self.calls.append(("list", prefix))
        return iter([SimpleNamespace(pathname=k) for k in sorted(self.objects) if k.startswith(prefix)])

    def delete(self, path):
        self.calls.append(("delete", path))
        self.objects.pop(path, None)


def test_a_blob_key_is_written_once_and_the_same_bytes_again_are_a_rerun():
    client = FakeBlobClient()
    store = VercelBlobObjectStore(client)
    first = store.put_immutable("surge/phase-b/x/a.json", b"{}\n", "application/json")
    assert first.created and first.sha256 == sha256_hex(b"{}\n")
    again = store.put_immutable("surge/phase-b/x/a.json", b"{}\n", "application/json")
    assert not again.created and again.sha256 == first.sha256
    with pytest.raises(ImmutableObjectConflict, match="different content"):
        store.put_immutable("surge/phase-b/x/a.json", b"[]\n", "application/json")
    assert client.objects["surge/phase-b/x/a.json"] == b"{}\n"  # never overwritten
    assert store.operations.advanced == 3 and store.operations.simple == 2  # each refusal is verified by a read


def test_a_put_whose_answer_was_lost_is_recognised_as_ours():
    store = VercelBlobObjectStore(FakeBlobClient(land_then_refuse=True))
    stored = store.put_immutable("surge/phase-b/x/b.json", b"1\n", "application/json")
    assert not stored.created and stored.sha256 == sha256_hex(b"1\n")


def test_blob_reads_existence_and_keys():
    client = FakeBlobClient()
    store = VercelBlobObjectStore(client)
    assert store.exists("surge/k.json") is False and store.head("surge/k.json") is None
    with pytest.raises(ObjectStoreError, match="not in the store"):
        store.get("surge/k.json")
    store.put_immutable("surge/k.json", b"x", "text/plain")
    before = len([c for c in client.calls if c[0] == "get"])
    assert store.exists("surge/k.json") is True
    assert len([c for c in client.calls if c[0] == "get"]) == before  # a head, not a download
    assert store.get("surge/k.json") == b"x" and store.head("surge/k.json").sha256 == sha256_hex(b"x")
    assert list(store.list("surge/")) == ["surge/k.json"]
    for bad in ("", "/abs", "a/../b", "a\\b"):
        with pytest.raises(ValueError):
            store.put_immutable(bad, b"x", "text/plain")
    assert store.delete("surge/k.json") is True and store.delete("surge/k.json") is False


def test_blob_is_never_the_default_and_names_the_variable_not_a_value(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    with pytest.raises(ObjectStoreError, match="BLOB_READ_WRITE_TOKEN"):
        open_store("vercel-blob", env={})
    with pytest.raises(ObjectStoreError, match="no object store selected"):
        open_store(None, env={})


# ----------------------------------------------------------------- the artifact format


def test_json_is_serialized_as_the_pc_writes_it(tmp_path):
    payload = {"b": 1, "a": ["x", "名前"], "d": date(2026, 9, 24)}
    rows = [{"code": "3000", "name": "銘柄"}, {"code": "3001", "v": 1.5}]
    store = RunStore(tmp_path, "run")
    assert sha256_hex(json_bytes(payload)) == store.write_json("p.json", payload)
    assert sha256_hex(jsonl_bytes(rows)) == store.write_jsonl("r.jsonl", rows)
    assert artifacts.gzip_bytes(b"same") == artifacts.gzip_bytes(b"same")  # no timestamp in the object


def test_a_set_is_read_only_through_its_commit_record(tmp_path):
    store = LocalObjectStore(tmp_path)
    prefix = artifacts.day_prefix("phase-b-test", date(2026, 9, 24))
    assert prefix == "surge/phase-b/phase-b-test/2026-09-24"
    parts = ArtifactSet(store, prefix)
    parts.put_json("manifest.json", {"s0": "2026-09-24"})
    parts.put_gzip("screening.jsonl.gz", jsonl_bytes([{"code": "3000"}]))
    with pytest.raises(ImmutableObjectConflict):
        parts.put_json("manifest.json", {"s0": "other"})
    with pytest.raises(ValueError):
        parts.put_json("../escape.json", {})
    parts.put("notes.txt", b"x", "text/plain")
    with pytest.raises(ValueError):
        parts.put_gzip("not-gz.json", b"x")
    assert read_commit(store, prefix, DAY_COMMIT) is None  # not a day until committed
    commit = parts.commit(DAY_COMMIT, {"status": "sent"}, committed_at="2026-09-24T09:00:00+00:00")
    record = read_commit(store, prefix, DAY_COMMIT)
    assert record["status"] == "sent" and set(record["artifacts"]) == {"manifest.json", "screening.jsonl.gz",
                                                                        "notes.txt"}
    assert commit.sha256 == sha256_hex((tmp_path / prefix / DAY_COMMIT).read_bytes())
    assert json.loads(read_verified(store, prefix, record, "screening.jsonl.gz")) == {"code": "3000"}
    assert verify_committed(store, prefix, record) == []
    with pytest.raises(IntegrityError, match="not in its commit record"):
        read_verified(store, prefix, record, "predictions.jsonl")
    (tmp_path / prefix / "manifest.json").write_bytes(b'{"s0": "tampered"}\n')
    assert verify_committed(store, prefix, record) == [
        "manifest.json: surge/phase-b/phase-b-test/2026-09-24/manifest.json does not match its commit record"]
    with pytest.raises(ImmutableObjectConflict):  # the commit point is written once
        parts.commit(DAY_COMMIT, {"status": "stopped"}, committed_at="2026-09-25T09:00:00+00:00")


def test_run_records_take_the_first_free_number_of_their_jst_day(tmp_path):
    store = LocalObjectStore(tmp_path)
    late_utc = datetime(2026, 9, 24, 15, 30, tzinfo=UTC)  # 00:30 on the 25th in Tokyo
    first = put_run_record(store, "phase-b-test", "prediction", {"status": "sent"}, started_at=late_utc)
    assert first == "surge/phase-b/phase-b-test/runs/2026-09-25/prediction-1.json"
    assert put_run_record(store, "phase-b-test", "prediction", {"status": "sent"}, started_at=late_utc) == first
    second = put_run_record(store, "phase-b-test", "prediction", {"status": "locked"}, started_at=late_utc)
    assert second.endswith("runs/2026-09-25/prediction-2.json")
    with pytest.raises(ValueError):
        put_run_record(store, "phase-b-test", "Bad Job", {}, started_at=late_utc)
    with pytest.raises(ValueError):
        artifacts.cohort_prefix("../other")


# ----------------------------------------------------------------- the export of a cohort the PC wrote


def _machine_markers() -> list[bytes]:
    """This machine's paths, as written and as JSON escapes them."""

    paths = [str(REPO_ROOT), str(Path.home())]
    return [form.encode() for p in paths for form in (p, json.dumps(p)[1:-1])]


def _all_bytes(root: Path) -> bytes:
    chunks = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            chunks.append(gzip.decompress(data) if path.suffix == ".gz" else data)
    return b"\n".join(chunks)


def test_an_exported_day_keeps_the_pc_s_rows_and_hashes_and_no_request_body(tmp_path, monkeypatch):
    env = t._setup(tmp_path / "pc", monkeypatch, until=date(2026, 11, 30), holidays=True)
    day = t._send_day(env)
    t._outcome_run(env, datetime(2026, 10, 23, 18, 0, tzinfo=JST))
    planned_only = date(2026, 9, 25)
    t._plan(env, planned_only, clock=lambda: t._evening(planned_only))  # in progress: not final
    target = LocalObjectStore(tmp_path / "blob")
    written = export_cohort(env.root, t.COHORT, target, system="synthetic-fixture",
                            exported_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC))

    assert {d["s0"]: d["exported"] for d in written["days"]} == {"2026-09-24": True, "2026-09-25": False}
    prefix = artifacts.day_prefix(t.COHORT, t.S0)
    commit = read_commit(target, prefix, DAY_COMMIT)
    assert commit["status"] == "sent" and commit["system"] == "synthetic-fixture" and commit["official"] is False
    assert verify_committed(target, prefix, commit) == []
    plan, build, run = (day.read_json(f"stage-{s}.json")["files_sha256"] for s in ("plan", "build", "run"))
    entries = commit["artifacts"]
    assert entries["population.jsonl"]["sha256"] == plan["population.jsonl"]
    assert entries["candidates.jsonl.gz"]["content_sha256"] == plan["candidates.jsonl"]
    assert entries["inputs/universe.json.gz"]["content_sha256"] == plan["inputs/universe.json"]
    assert entries["manifest.json"]["sha256"] == plan["manifest.json"]
    assert entries["requests.jsonl"]["sha256"] == build["requests.jsonl"]
    assert entries["predictions.jsonl"]["sha256"] == run["predictions.jsonl"]
    assert commit["source"]["files_sha256"]["population.jsonl"] == plan["population.jsonl"]

    population = [json.loads(line) for line in read_verified(target, prefix, commit, "population.jsonl").splitlines()]
    selected = [json.loads(line) for line in
                read_verified(target, prefix, commit, "inputs/prices-selected.jsonl.gz").splitlines()]
    assert {row["code"] for row in selected} == {row["code"] for row in population}
    digests = json.loads(read_verified(target, prefix, commit, "inputs/price-digests.json"))
    with gzip.open(day.path / "inputs" / "prices.jsonl.gz", "rb") as archive:
        lines = [line for line in archive if line.strip()]
    assert digests["lines"] == {json.loads(line)["code"]: sha256_hex(line) for line in lines}
    responses = [json.loads(line) for line in read_verified(target, prefix, commit, "responses.jsonl.gz").splitlines()]
    assert len(responses) == 2 * len(day.read_jsonl("requests.jsonl"))  # each answer and its raw body
    tdnet = [json.loads(line) for line in read_verified(target, prefix, commit, "inputs/tdnet.jsonl.gz").splitlines()]
    assert {r["file"]: r["sha256"] for r in tdnet} == {k: v for k, v in build.items() if k.startswith("inputs/tdnet/")}

    outcome = read_commit(target, prefix, OUTCOME_COMMIT)
    assert outcome["stage"]["t_plus_20"] == "2026-10-23"
    assert read_verified(target, prefix, outcome, "outcomes.jsonl") == (
        day.path / day.read_json("stage-outcomes.json")["outcomes_file"]).read_bytes()

    everything = _all_bytes(tmp_path / "blob")
    assert b"canonical_v5_1" not in everything  # no request body, so no Canonical text
    assert not [m for m in _machine_markers() if m in everything]
    assert b'"pid"' not in everything and b"traceback" not in everything
    runs = sorted((tmp_path / "blob").rglob("runs/*/*.json"))
    assert [p.parent.name for p in runs] == ["2026-10-23"]
    record = json.loads(runs[0].read_text(encoding="utf-8"))
    assert record["job"] == "outcome" and set(record["code"]) == {"head", "expected_commit"}

    again = export_cohort(env.root, t.COHORT, target, system="synthetic-fixture",
                          exported_at=datetime(2026, 10, 24, 12, 0, tzinfo=UTC))
    assert {d["reason"] for d in again["days"]} == {"already committed", "not final (neither sent nor stopped)"}
    assert again["runs"] == written["runs"]  # the same records land on the same keys


def test_a_stopped_day_is_exported_with_its_reason_and_a_machine_path_never_is(tmp_path, monkeypatch):
    env = t._setup(tmp_path / "pc", monkeypatch, until=date(2026, 11, 30), holidays=True)
    record = t._scheduled(env, datetime(2026, 9, 24, 16, 10, 5, tzinfo=JST),
                          yahoo=t._FlakyYahoo(env.charts, {"3001.T", "3002.T", "3003.T"}))
    assert record["status"] == "stopped"
    target = LocalObjectStore(tmp_path / "blob")
    export_cohort(env.root, t.COHORT, target, system="synthetic-fixture")
    prefix = artifacts.day_prefix(t.COHORT, t.S0)
    commit = read_commit(target, prefix, DAY_COMMIT)
    assert commit["status"] == "stopped" and set(commit["artifacts"]) == {"run.json"}
    run = json.loads(read_verified(target, prefix, commit, "run.json"))
    assert "below 95%" in run["day_stopped"][0]["reason"]
    assert scrub({"detail": r"read C:\Users\someone\x.json and /home/u/y failed", "pid": 7}) == {
        "detail": "read <local path> and <local path> failed"}
    day = phase_b.day_store(env.root, t.COHORT, date(2026, 9, 25))
    t._plan(env, date(2026, 9, 25), clock=lambda: t._evening(date(2026, 9, 25)))
    day.write_json("day-stopped-1.json", {"reason": "x"})
    manifest = json.loads((day.path / "manifest.json").read_text(encoding="utf-8"))
    (day.path / "manifest.json").unlink()
    day.write_json("manifest.json", {**manifest, "note": "written on C:\\Users\\someone"})
    with pytest.raises(ExportError, match="names a local path"):
        export_cohort(env.root, t.COHORT, target, system="synthetic-fixture")
    with pytest.raises(ExportError, match="unknown system"):
        export_cohort(env.root, t.COHORT, target, system="pc")


# ----------------------------------------------------------------- the committed fixture


def test_the_committed_fixture_is_whole_synthetic_and_names_no_machine():
    store = LocalObjectStore(FIXTURE)
    meta = json.loads(store.get("fixture.json"))
    assert meta["not_real_data"] is True and meta["cohort_id"] == FIXTURE_COHORT
    base = artifacts.cohort_prefix(FIXTURE_COHORT)
    cohort = json.loads(store.get(f"{base}/cohort.json"))
    assert cohort["cohort_id"] == FIXTURE_COHORT and cohort["experiment_seed"] != "surge-phase-b-jp-jev-1.13.0-v1-20260924"
    days = sorted(p.name for p in (FIXTURE / base).iterdir() if p.name[:4].isdigit())
    assert days
    for s0 in days:
        prefix = f"{base}/{s0}"
        commit = read_commit(store, prefix, DAY_COMMIT)
        assert commit["system"] == "synthetic-fixture" and commit["official"] is False
        assert verify_committed(store, prefix, commit) == []
        outcome = read_commit(store, prefix, OUTCOME_COMMIT)
        if outcome is not None:
            assert verify_committed(store, prefix, outcome) == []
    everything = _all_bytes(FIXTURE)
    assert b"canonical_v5_1" not in everything and b'"pid"' not in everything and b"traceback" not in everything
    assert not [m for m in _machine_markers() if m in everything]
