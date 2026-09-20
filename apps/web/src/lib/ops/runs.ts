/**
 * Every run the cloud has done, of whatever kind (D-279).
 *
 * A day and a rehearsal write artifacts, so they are read from their cohort's
 * own records; a no-op writes a status record; a probe and a comparison are
 * published by the machine that ran them. They are shown in one list because
 * an operator asks the same questions of all of them: what did it do, how long
 * did it take, what did it cost, and why did it stop.
 */

import {
  type CloudStatus,
  type CompareStatus,
  type PcStatus,
  type Published,
} from "@/lib/ops/status";
import { type Day, type Shadow, loadDays, openShadow } from "@/lib/shadow/model";
import { type ArtifactSource } from "@/lib/shadow/source";

export type RunKind = "shadow day" | "rehearsal" | "probe" | "no-op" | "compare";

export interface RunRow {
  kind: RunKind;
  what: string;
  status: string;
  startedAt: string | null;
  completedAt: string | null;
  seconds: number | null;
  universe: number | null;
  coverage: number | null;
  passing: number | null;
  primary: number | null;
  control: number | null;
  events: number | null;
  cpuSeconds: number | null;
  memoryMb: number | null;
  stopReason: string | null;
  href?: string;
  detail?: string;
}

interface ShadowRun {
  mode?: string;
  trigger?: string;
  requested_at?: string;
  workflow_run_id?: string;
  steps?: {
    universe?: { began?: string; ended?: string; issues?: number };
    chunks?: { seconds?: number; cpu_seconds?: number; max_rss_mb?: number; read?: number; failed?: number }[];
    plan?: { began?: string; ended?: string };
    reread?: { seconds?: number };
    build?: { began?: string; ended?: string; cpu_seconds?: number };
  };
}

function elapsed(from: string | null | undefined, to: string | null | undefined): number | null {
  if (!from || !to) return null;
  const seconds = (Date.parse(to) - Date.parse(from)) / 1000;
  return Number.isFinite(seconds) ? Math.round(seconds) : null;
}

function dayRow(shadow: Shadow, day: Day): RunRow {
  const plan = day.run?.stages?.plan;
  const run = (day.run as unknown as { shadow?: ShadowRun } | null)?.shadow;
  const chunks = run?.steps?.chunks ?? [];
  const started = run?.requested_at ?? day.run?.run_started?.[0]?.started_at ?? null;
  const stopped = day.run?.day_stopped?.[0];
  return {
    kind: shadow.which === "rehearsal" ? "rehearsal" : "shadow day",
    what: `S0 ${day.s0}`,
    status: day.status,
    startedAt: started,
    completedAt: String(day.commit.committed_at ?? "") || null,
    seconds: elapsed(started, String(day.commit.committed_at ?? "")),
    universe: plan?.issues ?? null,
    coverage: plan?.coverage ?? null,
    passing: plan?.passing ?? null,
    primary: plan?.selected?.primary ?? null,
    control: plan?.selected?.control ?? null,
    events: null,
    cpuSeconds: chunks.length ? round(chunks.reduce((sum, c) => sum + (c.cpu_seconds ?? 0), 0)) : null,
    memoryMb: chunks.length ? Math.max(...chunks.map((c) => c.max_rss_mb ?? 0)) || null : null,
    stopReason: stopped?.reason ?? null,
    href: `/cohort/days?cohort=${shadow.which}`,
    detail: chunks.length ? `${chunks.length} read steps, slowest ${Math.max(...chunks.map((c) => c.seconds ?? 0))}s` : undefined,
  };
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}

function noopRow(entry: Published<CloudStatus>): RunRow {
  const record = entry.record;
  return {
    kind: "no-op",
    what: record.cron ? `cron (${record.trigger.replace(/^cron /, "")})` : record.trigger,
    status: "completed",
    startedAt: record.published_at,
    completedAt: record.published_at,
    seconds: null,
    universe: null,
    coverage: null,
    passing: null,
    primary: null,
    control: null,
    events: null,
    cpuSeconds: null,
    memoryMb: null,
    stopReason: null,
    detail: `${record.deployment.commit?.slice(0, 7) ?? "?"} · ${record.run_id ?? ""}`,
  };
}

function probeRows(pc: PcStatus): RunRow[] {
  const checks = pc.checks.before_the_deployment.checks ?? {};
  return Object.entries(checks).map(([name, check]) => {
    const measure = (check.measure ?? {}) as Record<string, number>;
    const workflow = (check.workflow ?? {}) as { events?: number };
    return {
      kind: "probe" as const,
      what: String(check.check ?? name),
      status: check.ok ? "passed" : "failed",
      startedAt: pc.checks.before_the_deployment.at,
      completedAt: pc.checks.before_the_deployment.at,
      seconds: measure.seconds ?? null,
      universe: null,
      coverage: null,
      passing: null,
      primary: null,
      control: null,
      events: workflow.events ?? null,
      cpuSeconds: measure.cpu_seconds ?? null,
      memoryMb: measure.max_rss_mb ?? null,
      stopReason: check.ok ? null : "see the check's own record",
      detail: measure.attempted ? `${measure.read}/${measure.attempted} read, ${measure.failed} failed` : undefined,
    };
  });
}

function rehearsalRow(pc: PcStatus): RunRow | null {
  const rehearsal = pc.checks.rehearsal as Record<string, unknown>;
  if (!rehearsal?.run_id && !rehearsal?.status) return null;
  const summary = (rehearsal.summary ?? {}) as Record<string, number> & { selected?: Record<string, number> };
  const steps = (rehearsal.steps ?? {}) as ShadowRun["steps"];
  const chunks = steps?.chunks ?? [];
  return {
    kind: "rehearsal",
    what: `S0 ${String(rehearsal.s0 ?? "—")} (published)`,
    status: String(rehearsal.status ?? "—"),
    startedAt: steps?.universe?.began ?? null,
    completedAt: steps?.build?.ended ?? null,
    seconds: elapsed(steps?.universe?.began, steps?.build?.ended),
    universe: summary.issues ?? null,
    coverage: summary.coverage ?? null,
    passing: summary.passing ?? null,
    primary: summary.selected?.primary ?? null,
    control: summary.selected?.control ?? null,
    events: null,
    cpuSeconds: chunks.length ? round(chunks.reduce((sum, c) => sum + (c.cpu_seconds ?? 0), 0)) : null,
    memoryMb: chunks.length ? Math.max(...chunks.map((c) => c.max_rss_mb ?? 0)) || null : null,
    stopReason: (rehearsal.refused as { message?: string } | null)?.message ?? null,
    href: "/cohort?cohort=rehearsal",
  };
}

function compareRow(entry: Published<CompareStatus>): RunRow {
  const record = entry.record;
  const items = Object.values(record.items);
  const unexplained = items.filter((item) => item.status === "unexplained_mismatch").length;
  return {
    kind: "compare",
    what: `S0 ${record.s0}: the PC's day against the cloud's`,
    status: unexplained ? `${unexplained} unexplained` : "every item accounted for",
    startedAt: record.published_at,
    completedAt: record.published_at,
    seconds: null,
    universe: null,
    coverage: null,
    passing: null,
    primary: null,
    control: null,
    events: null,
    cpuSeconds: null,
    memoryMb: null,
    stopReason: null,
    href: "/compare",
    detail: `${items.length} items compared`,
  };
}

/** Every run, newest first. Days and rehearsals come from their artifacts; the rest from what was published. */
export async function loadRunRows(source: ArtifactSource, cloudHistory: Published<CloudStatus>[],
                                  pc: PcStatus | null, compare: Published<CompareStatus> | null): Promise<RunRow[]> {
  const rows: RunRow[] = [];
  for (const which of ["shadow", "rehearsal"] as const) {
    const shadow = await openShadow(source, which);
    if (!shadow.cohort) continue;
    const days = await loadDays(shadow);
    rows.push(...days.map((day) => dayRow(shadow, day)));
  }
  rows.push(...cloudHistory.map(noopRow));
  if (pc) {
    rows.push(...probeRows(pc));
    const rehearsal = rehearsalRow(pc);
    if (rehearsal && !rows.some((row) => row.kind === "rehearsal" && row.what.startsWith(rehearsal.what.slice(0, 11)))) {
      rows.push(rehearsal);
    }
  }
  if (compare) rows.push(compareRow(compare));
  return rows.sort((a, b) => (b.startedAt ?? "").localeCompare(a.startedAt ?? ""));
}
