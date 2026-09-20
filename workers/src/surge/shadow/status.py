"""What the shadow can say about itself, written where the screens can read it (D-279).

The screens show the system as it is, not a fixture, and nothing they need is in
a database: the deployment's own identity comes from its environment, the code's
identity from the frozen code it carries, and everything else (the PC's Phase B,
the free quota the dashboard alone shows, the checks run before a day) is
published by whoever knows it.

A record is written the way a run record is - ``surge/status/operations/runs/<date>/<job>-<n>.json``,
write-once, its key derived from the date and a number - so a reader never lists
the store (a list is a billed operation on Hobby) and an old record is never
rewritten. ``cloud`` records are written by this service, ``pc`` records by the
operator's machine; the screens read the newest of each.
"""

from __future__ import annotations

import os
import platform
from datetime import UTC, datetime
from importlib import metadata

from surge.shadow.artifacts import put_run_record
from surge.storage.base import ObjectStore

STATUS_ROOT = "surge/status"
#: Not a cohort: the prefix the operational records share, so their keys are derived like a cohort's.
STATUS_COHORT = "operations"
CLOUD, PC = "cloud", "pc"
#: Set on the deployment (`vercel deploy --env SURGE_COMMIT=...`): a CLI deployment carries no git metadata.
COMMIT_ENV = "SURGE_COMMIT"


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def deployment() -> dict:
    """The deployment as Vercel's own environment describes it. No secret is read here."""

    return {
        "id": os.environ.get("VERCEL_DEPLOYMENT_ID"),
        "commit": os.environ.get(COMMIT_ENV),
        "url": os.environ.get("VERCEL_URL"),
        "target": os.environ.get("VERCEL_TARGET_ENV") or os.environ.get("VERCEL_ENV"),
        "region": os.environ.get("VERCEL_REGION"),
        "python": platform.python_version(),
        "packages": {name: _version(name) for name in ("vercel", "vercel-workflow", "curl_cffi", "openpyxl",
                                                        "tiktoken")},
    }


def cloud_record(trigger: str, *, now: datetime | None = None, **extra) -> dict:
    """What this deployment is and what it is bound to: written on every no-op, so the cron keeps it fresh."""

    from surge.evaluation import phase_b
    from surge.evaluation.method import load_method
    from surge.jobs.jev_eval import EVALUATION_VERSION, code_version
    from surge.shadow import day as shadow_day

    now = now or datetime.now(UTC)
    fingerprint = phase_b.fingerprint(phase_b.frozen_protocol(load_method(), evaluation_version=EVALUATION_VERSION))
    schedule = shadow_day.scheduled_s0(now)
    return {
        "kind": CLOUD,
        "published_at": now.isoformat(),
        "trigger": trigger,
        "cron": trigger.startswith("cron"),
        "deployment": deployment(),
        "code": {"evaluation_version": EVALUATION_VERSION,
                 "input_building_code_sha256": code_version()["code_sha256"],
                 "frozen_protocol_fingerprint": fingerprint,
                 "matches_official_cohort": fingerprint == shadow_day.OFFICIAL.frozen_fingerprint},
        "cohorts": {name: {"cohort_id": binding.cohort_id, "root": root, "system": system}
                    for name, (binding, root, system) in shadow_day.MODES.items()},
        "phase_b": {"prospective_start": phase_b.PHASE_B_START.isoformat(),
                    "model": phase_b.PINNED_MODEL,
                    "next_s0": schedule["s0"], "window_open": schedule["window_open"],
                    "opens_at": schedule["opens_at"], "closes_at": schedule["closes_at"],
                    "jev": shadow_day.JEV},
        **extra,
    }


def publish(store: ObjectStore, record: dict, *, job: str = CLOUD, now: datetime | None = None) -> str:
    """One record, on its own key. The newest of a job is the highest number of the latest date."""

    return put_run_record(store, STATUS_COHORT, job, record, started_at=now or datetime.now(UTC), root=STATUS_ROOT)


__all__ = ["CLOUD", "COMMIT_ENV", "PC", "STATUS_COHORT", "STATUS_ROOT", "cloud_record", "deployment", "publish"]
