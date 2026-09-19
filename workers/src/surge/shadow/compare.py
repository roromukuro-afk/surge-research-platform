"""A shadow day against the PC's day of the same S0 (D-279, Stage 3).

What the user asked to agree before Jev is called from the cloud: the universe,
the population at no more than 3,000 yen, the screening pass, the route
memberships, Primary, Control and the request build hashes. Each is compared
here, and each disagreement is shown, not summarized away.

The request hashes are compared twice (vercel-shadow.md section 4):

- **same input** - the cloud's recorded inputs (its prices, its disclosure
  index answers, its plan) built again here by the frozen ``build``: the bytes
  must be the cloud's. This is the code: it says the cloud builds what the PC's
  code builds from the same input;
- **independent input** - the cloud's hashes against the PC's. This is the
  data: a price history or a disclosure title read differently gives another
  request, and each such request is traced to its history or its titles. A
  title published between the two reads is a legitimate difference (the frozen
  protocol reads the index when the day is built); it is still a difference.

It reads the PC's day and the cloud's artifacts and writes nothing.

    python -m surge.shadow.compare --s0 2026-09-24 [--cohort-id C] [--root <PC evaluation root>]
                                   [--cloud vercel-blob | local:<dir>] [--json <file>]
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from surge.evaluation import phase_b
from surge.evaluation.prices import history_from_chart
from surge.evaluation.state import select_disclosures
from surge.evaluation.store import default_root
from surge.shadow import day as shadow_day
from surge.shadow.artifacts import DAY_COMMIT, day_prefix, gzip_bytes, read_commit, read_verified
from surge.storage.base import ObjectStore

#: How many disagreeing securities or requests a report lists for each item (the counts are always complete).
SHOWN = 25


class RecordedYanoshin:
    """Yanoshin's per-code index as a day recorded it, answering again.

    A record keeps the security's own rows and how many rows the index returned
    with the oldest one's time: the other issuers' rows are stood in for, so that
    ``returned`` and ``oldest_returned`` - which decide the coverage - come out
    as recorded.
    """

    def __init__(self, records: dict[str, dict]):
        self.records = records
        limits = {r["limit"] for r in records.values()}
        self.limit = limits.pop() if len(limits) == 1 else 300

    def fetch_for_codes(self, codes):
        (code,) = codes
        record = self.records[code]
        items = [SimpleNamespace(yanoshin_id=row["id"], pubdate=datetime.fromisoformat(row["pubdate"]),
                                 title=row["title"], code=SimpleNamespace(normalised=code))
                 for row in record["items"]]
        others = record["returned"] - len(items)
        if others > 0:
            oldest = datetime.fromisoformat(record["oldest_returned"])
            items += [SimpleNamespace(yanoshin_id=-n, pubdate=oldest, title="", code=SimpleNamespace(normalised=""))
                      for n in range(1, others + 1)]
        return SimpleNamespace(items=items, fetched_at=datetime.fromisoformat(record["fetched_at"]),
                               endpoint=record["endpoint"], response_sha256=record["response_sha256"],
                               total_count=record["total_count"])


# ----------------------------------------------------------------- reading both sides


def _jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def pc_day(root: Path, cohort_id: str, s0: date) -> dict:
    """The PC's day as its RunStore holds it; nothing is written."""

    store = phase_b.day_store(root, cohort_id, s0)
    if not store.exists("manifest.json"):
        raise FileNotFoundError(f"the PC has no planned day {store.run_id} under {root}")
    tdnet = {}
    for path in sorted((store.path / "inputs" / "tdnet").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        tdnet[record["code"]] = record
    prices = {}
    archive = store.path / "inputs" / "prices.jsonl.gz"
    population = store.read_jsonl("population.jsonl")
    wanted = {row["code"] for row in population}
    if archive.exists():
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            for line in handle:
                payload = json.loads(line)
                if payload["code"] in wanted:
                    prices[payload["code"]] = payload
    return {
        "store": store,
        "manifest": store.read_json("manifest.json"),
        "universe": store.read_json("inputs/universe.json"),
        "sessions": store.read_json("sessions.json"),
        "candidates": store.read_jsonl("candidates.jsonl"),
        "population": population,
        "requests": store.read_jsonl("requests.jsonl") if store.exists("requests.jsonl") else [],
        "tdnet": tdnet,
        "prices": prices,
        "status": "sent" if store.exists("stage-run.json") else ("built" if store.exists("stage-build.json")
                                                                  else "planned"),
    }


def cloud_day(store: ObjectStore, cohort_id: str, s0: date, *, root: str = shadow_day.SHADOW_ROOT) -> dict:
    """The shadow's day, every artifact read against its integrity record."""

    prefix = day_prefix(cohort_id, s0, root=root)
    commit = read_commit(store, prefix, DAY_COMMIT)
    if commit is None:
        raise FileNotFoundError(f"no committed shadow day under {prefix}")

    def read(name: str) -> bytes | None:
        return read_verified(store, prefix, commit, name) if name in commit["artifacts"] else None

    digests = json.loads(read("inputs/price-digests.json") or b"{}")
    prices = {}
    for line in (read("inputs/prices-selected.jsonl.gz") or b"").decode("utf-8").splitlines(keepends=True):
        if line.strip():
            prices[json.loads(line)["code"]] = line
    tdnet = {row["record"]["code"]: row["record"] for row in _jsonl(read("inputs/tdnet.jsonl.gz") or b"")}
    return {
        "commit": commit,
        "files": {name: read(name) for name in ("manifest.json", "sessions.json", "population.jsonl",
                                                "inputs/universe.json.gz", "candidates.jsonl.gz",
                                                "screening.jsonl.gz", "requests.jsonl", "run.json")},
        "digests": digests,
        "prices": prices,
        "tdnet": tdnet,
    }


# ----------------------------------------------------------------- comparing


def _differences(pc: dict, cloud: dict) -> dict:
    """Keyed rows compared: which keys only one side has, and which differ."""

    only_pc, only_cloud = sorted(set(pc) - set(cloud)), sorted(set(cloud) - set(pc))
    changed = sorted(k for k in set(pc) & set(cloud) if pc[k] != cloud[k])
    return {"match": not (only_pc or only_cloud or changed), "pc": len(pc), "cloud": len(cloud),
            "only_pc": only_pc[:SHOWN], "only_cloud": only_cloud[:SHOWN], "different": len(changed),
            "examples": [{"key": k, "pc": pc[k], "cloud": cloud[k]} for k in changed[:SHOWN]]}


def _manifest_view(manifest: dict) -> dict:
    """The manifest without what legitimately differs: when it started, and the git checkout it ran from."""

    view = json.loads(json.dumps(manifest))
    view.pop("started_at", None)
    code = view.get("input_building_code") or {}
    code.pop("git_head", None)
    code.pop("git_dirty_in_these_files", None)
    return view


def rebuild_from_cloud_inputs(cloud: dict, *, s0: date, binding: shadow_day.CohortBinding) -> str:
    """The cloud's requests built again here, by the frozen ``build``, from the cloud's own recorded inputs."""

    files = cloud["files"]
    stages = json.loads(files["run.json"])["stages"]
    plan_stage = stages["plan"]
    planned = {
        "files": {"manifest.json": files["manifest.json"], "sessions.json": files["sessions.json"],
                  "population.jsonl": files["population.jsonl"],
                  "inputs/universe.json.gz": gzip_bytes(files["inputs/universe.json.gz"]),
                  "candidates.jsonl.gz": gzip_bytes(files["candidates.jsonl.gz"])},
        "summary": {k: v for k, v in plan_stage.items() if k not in ("stage", "finished_at", "files_sha256")},
        "finished_at": plan_stage["finished_at"],
    }
    built = shadow_day.build(planned, {"lines": cloud["prices"]}, s0=s0, binding=binding,
                             yanoshin=RecordedYanoshin(cloud["tdnet"]))
    return built["requests"]


def _history_digest(chart_payload: dict) -> str:
    history = history_from_chart(chart_payload["code"], chart_payload["symbol"], chart_payload["result"],
                                 fetched_at=datetime.fromisoformat(chart_payload["fetched_at"]))
    return shadow_day.history_digest(history)


def _chosen_titles(record: dict | None, s0: date) -> list[dict]:
    if record is None:
        return []
    items = [SimpleNamespace(pubdate=datetime.fromisoformat(i["pubdate"]), title=i["title"]) for i in record["items"]]
    return select_disclosures(items, s0)


def explain_request(code: str, *, pc: dict, cloud: dict, s0: date) -> dict:
    """Why a request's bytes differ: its price history, its disclosure titles, or neither (unexplained)."""

    reasons = []
    pc_history = _history_digest(pc["prices"][code]) if code in pc["prices"] else None
    cloud_history = (cloud["digests"].get("histories") or {}).get(code)
    if pc_history != cloud_history:
        reasons.append("price history")
    pc_titles, cloud_titles = _chosen_titles(pc["tdnet"].get(code), s0), _chosen_titles(cloud["tdnet"].get(code), s0)
    detail = {}
    if pc_titles != cloud_titles:
        reasons.append("disclosure titles")
        pc_read = (pc["tdnet"].get(code) or {}).get("fetched_at")
        detail["titles_only_pc"] = [t for t in pc_titles if t not in cloud_titles]
        detail["titles_only_cloud"] = [t for t in cloud_titles if t not in pc_titles]
        detail["read_at"] = {"pc": pc_read, "cloud": (cloud["tdnet"].get(code) or {}).get("fetched_at")}
    return {"code": code, "reasons": reasons or ["unexplained"], **detail}


def compare(pc: dict, cloud: dict, *, s0: date, binding: shadow_day.CohortBinding = shadow_day.OFFICIAL) -> dict:
    files = cloud["files"]
    universe = json.loads(files["inputs/universe.json.gz"])
    candidates = _jsonl(files["candidates.jsonl.gz"])
    population = _jsonl(files["population.jsonl"])
    requests = _jsonl(files["requests.jsonl"])

    items = {}
    items["universe"] = {
        **_differences({i["code"]: i for i in pc["universe"]["issues"]}, {i["code"]: i for i in universe["issues"]}),
        "workbook_sha256": {"pc": pc["universe"]["workbook_sha256"], "cloud": universe["workbook_sha256"],
                            "match": pc["universe"]["workbook_sha256"] == universe["workbook_sha256"]},
        "histories_read": {"pc": pc["universe"]["histories_read"], "cloud": universe["histories_read"]},
        "failed": {"pc": [f["code"] for f in pc["universe"]["failures"]][:SHOWN],
                   "cloud": [f["code"] for f in universe["failures"]][:SHOWN]},
    }
    items["universe"]["match"] = items["universe"]["match"] and items["universe"]["workbook_sha256"]["match"]
    items["population_at_or_below_3000_yen"] = _differences(
        {c["code"]: c["close_as_traded"] for c in pc["candidates"]}, {c["code"]: c["close_as_traded"] for c in candidates})
    items["screening_pass"] = _differences({c["code"]: c["passed"] for c in pc["candidates"]},
                                           {c["code"]: c["passed"] for c in candidates})
    items["route_memberships"] = _differences({c["code"]: c["routes"] for c in pc["candidates"]},
                                              {c["code"]: c["routes"] for c in candidates})

    def cohort_rows(rows: list[dict], cohort: str) -> dict:
        keep = ("code", "route_d_subgroup", "selection_probability", "matched_to", "match_tier", "anonymized_pair",
                "drift_repeat")
        return {r["sample_id"]: {k: r.get(k) for k in keep} for r in rows if r["cohort"] == cohort}

    items["primary"] = _differences(cohort_rows(pc["population"], "PRIMARY"), cohort_rows(population, "PRIMARY"))
    items["control"] = _differences(cohort_rows(pc["population"], "CONTROL"), cohort_rows(population, "CONTROL"))
    items["sessions"] = {"match": pc["sessions"] == json.loads(files["sessions.json"])}
    items["manifest"] = {"match": _manifest_view(pc["manifest"]) == _manifest_view(json.loads(files["manifest.json"])),
                         "input_building_code_sha256": {
                             "pc": pc["manifest"]["input_building_code"]["code_sha256"],
                             "cloud": json.loads(files["manifest.json"])["input_building_code"]["code_sha256"]}}

    rebuilt = rebuild_from_cloud_inputs(cloud, s0=s0, binding=binding)
    items["request_hashes_same_input"] = {
        "match": rebuilt.encode("utf-8") == files["requests.jsonl"],
        "rule": "the cloud's recorded inputs built again here by the frozen build: the code, not the data",
    }
    pc_hashes = {f"{r['sample_id']}.{r['variant']}": r["sha256"] for r in pc["requests"]}
    cloud_hashes = {f"{r['sample_id']}.{r['variant']}": r["sha256"] for r in requests}
    independent = _differences(pc_hashes, cloud_hashes)
    codes = {row["sample_id"]: row["code"] for row in pc["population"]}
    differing = sorted({k.rsplit(".", 1)[0] for k in set(pc_hashes) & set(cloud_hashes)
                        if pc_hashes[k] != cloud_hashes[k]})
    independent["explained"] = [explain_request(codes[sample], pc=pc, cloud=cloud, s0=s0)
                                for sample in differing[:SHOWN]]
    independent["rule"] = "the cloud's hashes against the PC's: the data (prices and disclosure titles read apart)"
    items["request_hashes_independent_input"] = independent

    required = ("universe", "population_at_or_below_3000_yen", "screening_pass", "route_memberships", "primary",
                "control", "request_hashes_same_input", "request_hashes_independent_input")
    return {"s0": s0.isoformat(), "cohort_id": binding.cohort_id, "pc_status": pc["status"],
            "cloud_status": cloud["commit"]["status"], "items": items,
            "all_match": all(items[name]["match"] for name in required),
            "required": list(required)}


def render(report: dict) -> str:
    lines = [f"S0 {report['s0']}  cohort {report['cohort_id']}  (PC: {report['pc_status']}, cloud: "
             f"{report['cloud_status']})"]
    for name in report["required"] + ["sessions", "manifest"]:
        item = report["items"][name]
        extra = ""
        if "pc" in item and "cloud" in item and isinstance(item["pc"], int):
            extra = f"  pc {item['pc']} / cloud {item['cloud']}"
            if not item["match"]:
                extra += f"  only pc {len(item['only_pc'])}+, only cloud {len(item['only_cloud'])}+, " \
                         f"different {item['different']}"
        lines.append(f"  {'MATCH ' if item['match'] else 'DIFFER'} {name}{extra}")
    for explained in report["items"]["request_hashes_independent_input"].get("explained", []):
        lines.append(f"    {explained['code']}: {', '.join(explained['reasons'])}")
    lines.append("all required items match" if report["all_match"] else "NOT all required items match: no Jev smoke")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from surge.storage import open_store  # noqa: PLC0415 - the CLI's store, not the library's concern

    parser = argparse.ArgumentParser(description="Compare a shadow day with the PC's day of the same S0")
    parser.add_argument("--s0", required=True, type=date.fromisoformat)
    parser.add_argument("--cohort-id", default=shadow_day.OFFICIAL.cohort_id)
    parser.add_argument("--root", type=Path, help="the PC's evaluation root (default: SURGE_EVALUATION_ROOT or ~/.surge)")
    parser.add_argument("--cloud", default="vercel-blob", help="vercel-blob, or local:<dir> holding the artifacts")
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args(argv)
    if args.cohort_id != shadow_day.OFFICIAL.cohort_id:
        parser.error("the shadow reproduces the official cohort only")
    report = compare(pc_day(args.root or default_root(), args.cohort_id, args.s0),
                     cloud_day(open_store(args.cloud), args.cohort_id, args.s0), s0=args.s0)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(render(report))
    return 0 if report["all_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RecordedYanoshin", "cloud_day", "compare", "explain_request", "main", "pc_day",
           "rebuild_from_cloud_inputs", "render"]
