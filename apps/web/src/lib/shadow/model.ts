/**
 * The Phase B shadow as the screens see it: a cohort, its business days, the
 * scheduler's runs, outcomes and reports, read from the artifact store (D-279).
 *
 * Keys are derived, never listed: business days from the cohort's start and
 * the JPX calendar it was written with, run records by date and number,
 * reports and credit readings by number. A day is shown only once its
 * integrity record exists, and every artifact is read against it.
 *
 * Nothing here computes a metric the evaluation did not: counts are counted,
 * reports are shown as the report job wrote them.
 */

import {
  type ArtifactSource,
  type CommitRecord,
  jsonLines,
  readCommit,
  readJson,
  readVerified,
} from "@/lib/shadow/source";

export const DEFAULT_PREFIX = "surge/phase-b";
export const OFFICIAL_COHORT = "jev-phase-b-jp-20260924-v1";
export const JOBS = ["prediction", "outcome"] as const;

/**
 * Which cohort a screen is showing. `shadow` is the cloud's shadow of the PC's
 * Phase B (nothing under it until 2026-09-24), `rehearsal` is the cloud's own
 * run of a past day - its own cohort, never the evaluation's - and `demo` is
 * whatever synthetic cohort the environment names, which is made-up data.
 */
export const COHORTS = {
  shadow: { root: "surge/phase-b-shadow", cohortId: OFFICIAL_COHORT, label: "the shadow of the PC's Phase B" },
  rehearsal: { root: "surge/rehearsal", cohortId: "rehearsal-jp-vercel-v1", label: "the cloud rehearsal" },
  demo: { root: null, cohortId: null, label: "the synthetic fixture" },
} as const;

export type CohortName = keyof typeof COHORTS;

export function cohortName(value: string | undefined | null): CohortName {
  return value === "rehearsal" || value === "demo" || value === "shadow" ? value : "shadow";
}

// ----------------------------------------------------------------- shapes (as the Python side writes them)

export interface Cohort {
  cohort_id: string;
  created_at: string;
  experiment_seed: string;
  prospective_start: string;
  target_business_days: number;
  provider: string;
  requested_model: string;
  pinned_served_model: string;
  per_day: Record<string, number>;
  budget: { global_hard_cap_usd: string; daily_budget_usd: string; daily_max_requests: number; credit: string };
  send_window: string;
  pacing: string;
  frozen: Frozen;
  frozen_fingerprint: string;
  preregistered: Record<string, unknown>;
  not_introduced: string[];
  storage: Record<string, unknown>;
}

export interface Frozen {
  evaluation_version: string;
  method: {
    canonical: { path: string; sha256: string };
    addenda_newer_overrides_older: { path: string; sha256: string }[];
  };
  question_schema_hash: string;
  state_version: string;
  screener: Record<string, unknown>;
  selection: { version: string; prospective_start: string; per_day: Record<string, number> };
  outcome: { version: string; definition: unknown };
  decision_interpretation: Record<string, unknown>;
  files_sha256: Record<string, string | null>;
}

export interface Calendar {
  version: string;
  source: string;
  source_sha256: string;
  years: number[];
  closed_days: Record<string, string>;
}

export interface FixtureMeta {
  what: string;
  cohort_id: string;
  as_of: string;
  not_real_data: boolean;
}

export interface PlanStage {
  issues?: number;
  histories_read?: number;
  failed?: number;
  coverage?: number;
  eligible?: number;
  passing?: number;
  not_passing?: number;
  selected?: { primary: number; control: number; anonymized: number; drift: number };
  requests?: number;
  route_membership_among_passing?: Record<string, number>;
  route_d_subgroups_among_passing?: Record<string, number>;
  route_d_subgroups_selected?: Record<string, number>;
  control_match_tiers?: Record<string, number>;
  primary_selection_probability?: number;
}

export interface RunStage {
  provider?: string;
  served_model?: string;
  sent_this_time?: number;
  requests_answered?: number;
  spent_usd?: string;
  cohort_spent_usd?: string;
  cohort_cap_usd?: string;
  stopped?: unknown;
  finished_at?: string;
  credits_after?: Record<string, unknown>;
}

export interface RunJson {
  stages: { plan?: PlanStage & { finished_at?: string }; build?: Record<string, unknown>; run?: RunStage };
  preflight: Record<string, unknown>[];
  run_started: { started_at: string; pending?: number }[];
  run_stopped: Record<string, unknown>[];
  day_stopped: { stopped_at?: string; reason?: string; detail?: unknown }[];
}

export interface Sample {
  sample_id: string;
  cohort: "PRIMARY" | "CONTROL";
  code: string;
  name: string;
  segment: string;
  sector33: string;
  s0: string;
  s0_close_as_traded: string;
  routes: string[];
  anonymized_pair: boolean;
  drift_repeat: boolean;
  route_d_subgroup: string | null;
  selection_probability: number | null;
  match_tier: string | null;
}

export interface Prediction {
  sample_id: string;
  cohort: string;
  variant: "main" | "anonymized" | "drift";
  code: string;
  s0: string;
  status: string | null;
  error: unknown;
  sent_at: string | null;
  latency_seconds: number | null;
  served_model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  decision: string | null;
  decision_probabilities: Record<string, number> | null;
  decision_confidence: number | null;
  reaches_target: number | null;
  upside_score: number | null;
  upside_confidence: number | null;
  complete: boolean;
  route_d_subgroup: string | null;
  request_sha256: string;
}

export interface Outcome {
  sample_id: string;
  resolution: string;
  base: string;
  window_first: string;
  window_last: string;
  hit_20_high: boolean | null;
  hit_20_close: boolean | null;
  ret_t1: number | null;
  ret_t3: number | null;
  ret_t5: number | null;
  ret_t10: number | null;
  ret_t20: number | null;
  max_upside_high: number | null;
  max_upside_close: number | null;
  max_drawdown_low: number | null;
  max_drawdown_close: number | null;
  missing_sessions: string[];
}

export interface RunRecord {
  job: string;
  started_at: string;
  finished_at?: string;
  status: string;
  exit_code: number;
  s0?: string;
  today?: string;
  closed?: string | null;
  detail?: unknown;
  due?: string[];
  outcomes?: Record<string, { status: string; detail?: string; t_plus_20?: string }>;
  report?: { file?: string; status?: string; resolved?: number; unresolved?: number } | null;
  code?: { head?: string | null; expected_commit?: string | null };
  system?: string;
  /** Where it was read from, relative to the cohort. */
  key: string;
}

export interface Report {
  file: string;
  status: "partial" | "final";
  progress: {
    status: string;
    target_business_days: number;
    days_sent_in_full: number;
    days_with_outcomes: number;
    collection_rate: number;
    cohort_completion_rate: number;
    predictions: {
      sent: { PRIMARY: number; CONTROL: number; total: number };
      resolved: { PRIMARY: number; CONTROL: number; total: number };
      unresolved: {
        awaiting_t_plus_20: { total: number };
        missing_data: { total: number };
        total: { PRIMARY: number; CONTROL: number; total: number };
      };
    };
    days: { with_outcomes: string[]; awaiting_t_plus_20: { s0: string; t_plus_20_by_calendar: string }[] };
  };
  cohort: { reported_at: string; spent_usd: string; cap_usd: string };
  [section: string]: unknown;
}

export interface CreditReading {
  n: number;
  provider?: string;
  confirmed_balance_usd?: string;
  confirmed_at?: string;
  expires_at?: string;
  displayed_expiry?: string;
  source?: string;
  recorded_at?: string;
}

export interface Day {
  s0: string;
  commit: CommitRecord;
  status: string;
  system: string;
  official: boolean;
  run: RunJson | null;
  outcomeCommit: CommitRecord | null;
}

// ----------------------------------------------------------------- the calendar

const DAY_MS = 86_400_000;

/** The calendar date in Tokyo of an instant, as YYYY-MM-DD. */
export function jstDate(instant: Date): string {
  return new Date(instant.getTime() + 9 * 3_600_000).toISOString().slice(0, 10);
}

export function addDays(day: string, n: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
}

export function isWeekday(day: string): boolean {
  const weekday = new Date(`${day}T00:00:00Z`).getUTCDay();
  return weekday !== 0 && weekday !== 6;
}

/** True, false, or null for a year the calendar does not cover (never guessed). */
export function isBusinessDay(calendar: Calendar, day: string): boolean | null {
  if (!calendar.years.includes(Number(day.slice(0, 4)))) return null;
  return isWeekday(day) && !(day in calendar.closed_days);
}

export function weekdaysBetween(first: string, last: string): string[] {
  const days: string[] = [];
  for (let day = first; day <= last; day = addDays(day, 1)) if (isWeekday(day)) days.push(day);
  return days;
}

/** The n-th business day after `day` by the calendar, or null past what it covers. */
export function businessDayAfter(calendar: Calendar, day: string, n: number): string | null {
  let current = day;
  for (let found = 0; found < n; ) {
    current = addDays(current, 1);
    const business = isBusinessDay(calendar, current);
    if (business === null) return null;
    if (business) found++;
  }
  return current;
}

/** 16:10 JST on `day`: from then every bar of the session is final (D-262), the earliest a day can commit. */
export function closeConfirmedAt(day: string): Date {
  return new Date(`${day}T16:10:00+09:00`);
}

/** The next instant at or after `from` when a weekday job at hh:mm JST fires. */
export function nextWeekdayAt(from: Date, hour: number, minute: number): Date {
  let day = jstDate(from);
  for (let i = 0; i < 10; i++, day = addDays(day, 1)) {
    const at = new Date(`${day}T${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:00+09:00`);
    if (isWeekday(day) && at.getTime() >= from.getTime()) return at;
  }
  throw new Error("no weekday within ten days");
}

// ----------------------------------------------------------------- reading

export interface Shadow {
  source: ArtifactSource;
  which: CohortName;
  prefix: string;
  cohortId: string;
  cohort: Cohort | null;
  calendar: Calendar | null;
  fixture: FixtureMeta | null;
  now: Date;
}

export async function openShadow(source: ArtifactSource, which: CohortName = "shadow"): Promise<Shadow> {
  // A directory names the cohort it holds; a Blob store holds several, and each screen says which it wants.
  const directory = source.kind !== "blob" ? await readJson<FixtureMeta>(source, "fixture.json") : null;
  const chosen = COHORTS[which];
  const root = (chosen.root ?? process.env.SURGE_SHADOW_PREFIX ?? DEFAULT_PREFIX).replace(/^\/+|\/+$/g, "");
  const cohortId =
    chosen.cohortId ?? process.env.SURGE_SHADOW_COHORT ?? directory?.cohort_id ?? OFFICIAL_COHORT;
  const prefix = `${root}/${cohortId}`;
  const [cohort, calendar, marker] = await Promise.all([
    readJson<Cohort>(source, `${prefix}/cohort.json`),
    readJson<Calendar>(source, `${prefix}/calendar.json`),
    // A synthetic cohort carries its own marker, wherever it is stored: the label and the clock come with it.
    readJson<FixtureMeta>(source, `${prefix}/fixture.json`),
  ]);
  const fixture = marker ?? (directory?.cohort_id === cohortId ? directory : null);
  return { source, which, prefix, cohortId, cohort, calendar, fixture,
           now: fixture ? new Date(fixture.as_of) : new Date() };
}

/** A key under a day this long past can no longer be written: a miss there is remembered longer. */
function settled(shadow: Shadow, day: string): boolean {
  return day < addDays(jstDate(shadow.now), -1);
}

/**
 * Business days from the cohort's start to now, each read only through its
 * integrity record. A day is not asked for before its close is final, nor its
 * outcomes before their T+20 has closed by the calendar: those keys cannot
 * hold anything yet.
 */
export async function loadDays(shadow: Shadow): Promise<Day[]> {
  const { cohort, calendar, source, prefix } = shadow;
  if (!cohort || !calendar) return [];
  const candidates = weekdaysBetween(cohort.prospective_start, jstDate(shadow.now)).filter(
    (day) => isBusinessDay(calendar, day) !== false && closeConfirmedAt(day) <= shadow.now,
  );
  const days = await Promise.all(
    candidates.map(async (s0): Promise<Day | null> => {
      const commit = await readCommit(source, `${prefix}/${s0}`, "integrity.json", { settled: settled(shadow, s0) });
      if (!commit) return null;
      const due = businessDayAfter(calendar, s0, 20);
      const [runText, outcomeCommit] = await Promise.all([
        readVerified(source, commit, "run.json"),
        commit.status === "sent" && due !== null && closeConfirmedAt(due) <= shadow.now
          ? readCommit(source, `${prefix}/${s0}`, "outcomes-integrity.json")
          : Promise.resolve(null),
      ]);
      return {
        s0,
        commit,
        status: String(commit.status),
        system: String(commit.system),
        official: Boolean(commit.official),
        run: runText ? (JSON.parse(runText) as RunJson) : null,
        outcomeCommit,
      };
    }),
  );
  return days.filter((day): day is Day => day !== null);
}

export async function loadSamples(shadow: Shadow, day: Day): Promise<Sample[]> {
  return jsonLines<Sample>(await readVerified(shadow.source, day.commit, "population.jsonl"));
}

/** Every request's answer: the main ones, then the anonymized and drift repeats. */
export async function loadPredictions(shadow: Shadow, day: Day): Promise<Prediction[]> {
  const [main, variants] = await Promise.all([
    readVerified(shadow.source, day.commit, "predictions.jsonl"),
    readVerified(shadow.source, day.commit, "predictions-variants.jsonl"),
  ]);
  return [...jsonLines<Prediction>(main), ...jsonLines<Prediction>(variants)];
}

export async function loadOutcomes(shadow: Shadow, day: Day): Promise<Outcome[]> {
  if (!day.outcomeCommit) return [];
  return jsonLines<Outcome>(await readVerified(shadow.source, day.outcomeCommit, "outcomes.jsonl"));
}

async function numbered<T>(
  shadow: Shadow,
  key: (n: number) => string,
  limit = 200,
  isSettled = false,
): Promise<(T & { n: number })[]> {
  const found: (T & { n: number })[] = [];
  for (let n = 1; n <= limit; n++) {
    const value = await readJson<T>(shadow.source, key(n), { settled: isSettled });
    if (value === null) break;
    found.push({ ...value, n });
  }
  return found;
}

/** Every scheduled invocation, newest first: each weekday since the cohort was created, each job, 1..n. */
export async function loadRuns(shadow: Shadow): Promise<RunRecord[]> {
  if (!shadow.cohort) return [];
  const first = jstDate(new Date(shadow.cohort.created_at));
  const days = weekdaysBetween(first, jstDate(shadow.now));
  const perDay = await Promise.all(
    days.flatMap((day) =>
      JOBS.map(async (job) => {
        const records = await numbered<Omit<RunRecord, "key">>(
          shadow,
          (n) => `${shadow.prefix}/runs/${day}/${job}-${n}.json`,
          50,
          settled(shadow, day),
        );
        return records.map(({ n, ...record }) => ({ ...record, key: `runs/${day}/${job}-${n}.json` }));
      }),
    ),
  );
  return perDay.flat().sort((a, b) => b.started_at.localeCompare(a.started_at));
}

export async function loadReports(shadow: Shadow): Promise<Report[]> {
  const rolling = await numbered<Report>(shadow, (n) => `${shadow.prefix}/reports/report-${n}.json`);
  const final = await readJson<Report>(shadow.source, `${shadow.prefix}/reports/report-final.json`);
  return [...rolling.map(({ n: _n, ...report }) => report as Report), ...(final ? [final] : [])];
}

export async function loadCredit(shadow: Shadow): Promise<CreditReading[]> {
  return numbered<CreditReading>(shadow, (n) => `${shadow.prefix}/credit/typesafe-${n}.json`, 50);
}

export async function loadStops(shadow: Shadow): Promise<Record<string, unknown>[]> {
  return numbered<Record<string, unknown>>(shadow, (n) => `${shadow.prefix}/stops/cohort-stopped-${n}.json`, 20);
}

// ----------------------------------------------------------------- derived, for display only

export function stopReason(day: Day): string | null {
  const run = day.run;
  if (!run) return null;
  const stopped = run.day_stopped[run.day_stopped.length - 1]?.reason;
  if (stopped) return stopped;
  const runStopped = run.run_stopped[run.run_stopped.length - 1];
  if (runStopped) return String(runStopped.stopped ?? runStopped.reason ?? "stopped");
  const inRun = run.stages.run?.stopped;
  return inRun ? String(inRun) : null;
}

export function cohortSpend(days: Day[]): number {
  // The run stage records the cohort's spend so far; the latest day's is the total.
  let spent = 0;
  for (const day of days) {
    const value = Number(day.run?.stages.run?.cohort_spent_usd ?? NaN);
    if (!Number.isNaN(value)) spent = Math.max(spent, value);
  }
  return spent;
}

export function sentDays(days: Day[]): Day[] {
  return days.filter((day) => day.status === "sent");
}

export function isSynthetic(shadow: Shadow): boolean {
  return shadow.source.kind === "fixture" || Boolean(shadow.fixture?.not_real_data);
}
