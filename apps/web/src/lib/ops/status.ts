/**
 * The system as it is, for the screens that show it (D-279).
 *
 * Two halves are published as write-once records under
 * `surge/status/operations/runs/<date JST>/<job>-<n>.json`, exactly the way a
 * run record is written, so a reader derives every key and never lists the
 * store (a list is a billed operation on Hobby):
 *
 * - `cloud`: written by the deployment itself on every no-op, which the daily
 *   cron runs - its identity, the code it carries, what it is bound to and the
 *   next prospective S0 (`surge.shadow.status`);
 * - `pc`: written by the operator's machine (`surge-shadow-ops/publish_status.py`)
 *   - the official Phase B, its scheduled tasks, the credit and budget, the
 *   checks run before a day, the rehearsal, and the free quota a person read on
 *   the dashboard, which has no API;
 * - `compare`: written after the PC's day and the cloud's shadow of it are
 *   compared, item by item.
 *
 * Nothing here computes a fact of its own: a screen shows what was published
 * and when, or says that nothing has been.
 */

import { type ArtifactSource, readJson } from "@/lib/shadow/source";

export const STATUS_PREFIX = "surge/status/operations";
export const JOBS = ["cloud", "pc", "compare"] as const;
export type StatusJob = (typeof JOBS)[number];

/** How many days back a reader looks, and how many records of one day it will read. */
const DAYS_BACK = 14;
const PER_DAY = 20;

export interface Published<T> {
  key: string;
  day: string;
  n: number;
  record: T;
}

export interface CloudStatus {
  kind: "cloud";
  published_at: string;
  trigger: string;
  cron: boolean;
  run_id?: string;
  workflow?: string;
  deployment: {
    id: string | null;
    commit: string | null;
    url: string | null;
    target: string | null;
    region: string | null;
    python: string;
    packages: Record<string, string | null>;
  };
  code: {
    evaluation_version: string;
    input_building_code_sha256: string;
    frozen_protocol_fingerprint: string;
    matches_official_cohort: boolean;
  };
  cohorts: Record<string, { cohort_id: string; root: string; system: string }>;
  phase_b: {
    prospective_start: string;
    model: string;
    next_s0: string;
    window_open: boolean;
    opens_at: string;
    closes_at: string;
    jev: string;
  };
}

export interface PcStatus {
  kind: "pc";
  published_at: string;
  publisher: string;
  phase_b: {
    status: string;
    cohort_id: string;
    created_at: string;
    evaluation_version: string;
    frozen_protocol_fingerprint: string;
    prospective_start: string;
    first_eligible_s0: string;
    target_business_days: number;
    per_day: Record<string, number>;
    budget: Record<string, string | number>;
    send_window: string;
    pacing: string;
    model: { provider: string; requested: string; pinned_served: string };
    days: { sent: string[]; stopped: string[]; count: number };
    cohort_stopped: string[];
    spend: Record<string, string | number | null>;
    credit: Record<string, string>;
    frozen_worktree: { path: string; commit: string | null; clean: boolean };
    scheduler: { task: string; state: string; next_run: string; last_run: string; last_result: number }[];
  };
  cloud: {
    deployment: {
      id: string | null;
      commit: string | null;
      ready_state: string | null;
      target: string | null;
      created_at: number | null;
      aliases: string[] | null;
    };
    health: Record<string, unknown>;
  };
  checks: {
    before_the_deployment: {
      all_ok: boolean | null;
      at: string | null;
      s0: string | null;
      checks: Record<string, Record<string, unknown>>;
    };
    rehearsal: Record<string, unknown>;
    cron: Record<string, unknown>;
  };
  usage: Record<string, unknown>;
  shadow_budget?: { hard_cap_usd: string; spent_usd: string; rule: string };
}

/** As `surge.shadow.compare` writes it, with the two fields the publisher adds. */
export interface CompareStatus {
  kind: "compare";
  published_at: string;
  s0: string;
  cohort_id: string;
  pc_status: string;
  cloud_status: string;
  items: Record<string, { status: ItemStatus } & Record<string, unknown>>;
  stage4: {
    may_be_proposed: boolean;
    unexplained_by_group: Record<string, string[]>;
    rule: string;
  };
}

export type ItemStatus = "exact_match" | "expected_timing_difference" | "unexplained_mismatch";

function jstDay(at: Date, back: number): string {
  const shifted = new Date(at.getTime() + 9 * 3_600_000 - back * 86_400_000);
  return shifted.toISOString().slice(0, 10);
}

/** Every record of one job on one day, in the order they were written. */
async function recordsOfDay<T>(source: ArtifactSource, job: StatusJob, day: string,
                               settled: boolean): Promise<Published<T>[]> {
  const found: Published<T>[] = [];
  for (let n = 1; n <= PER_DAY; n++) {
    const key = `${STATUS_PREFIX}/runs/${day}/${job}-${n}.json`;
    const record = await readJson<T>(source, key, { settled });
    if (!record) break;
    found.push({ key, day, n, record });
  }
  return found;
}

/** The newest record a job has published, or null when it has published none. */
export async function newestStatus<T>(source: ArtifactSource, job: StatusJob,
                                      now: Date = new Date()): Promise<Published<T> | null> {
  for (let back = 0; back < DAYS_BACK; back++) {
    const day = jstDay(now, back);
    const records = await recordsOfDay<T>(source, job, day, back > 1);
    if (records.length) return records[records.length - 1];
  }
  return null;
}

/** Every record of a job over the window, newest first: what the cron and the publisher have written. */
export async function statusHistory<T>(source: ArtifactSource, job: StatusJob, days = 7,
                                       now: Date = new Date()): Promise<Published<T>[]> {
  const perDay = await Promise.all(
    Array.from({ length: days }, (_, back) => recordsOfDay<T>(source, job, jstDay(now, back), back > 1)),
  );
  return perDay.flat().reverse();
}

/** When a cron-triggered record was last written: the evidence that Vercel's schedule fires. */
export function cronVerifiedAt(history: Published<CloudStatus>[]): string | null {
  const fired = history.find((entry) => entry.record.cron);
  return fired ? fired.record.published_at : null;
}
