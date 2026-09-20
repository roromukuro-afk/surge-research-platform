/** Shared pieces of the Phase B shadow's screens (D-279). Server components only. */

import Link from "next/link";
import { type CohortName, type Shadow, COHORTS, isSynthetic, openShadow } from "@/lib/shadow/model";
import { shadowSource } from "@/lib/shadow/source";

/** The live system: what is running now, what has run, and how the cloud compares with the PC. */
export const OPS_TABS = [
  { href: "/", label: "Overview" },
  { href: "/system", label: "System" },
  { href: "/runs", label: "Runs" },
  { href: "/compare", label: "Compare" },
];

/** One cohort's own screens: the shadow's, the rehearsal's, or the synthetic fixture's. */
export const SHADOW_TABS = [
  { href: "/cohort", label: "Cohort" },
  { href: "/cohort/predictions", label: "Predictions" },
  { href: "/cohort/days", label: "Days" },
  { href: "/cohort/outcomes", label: "Outcomes" },
  { href: "/cohort/reports", label: "Reports" },
  { href: "/cohort/system", label: "Protocol" },
];

/** Keep the cohort a reader is looking at when they move between its screens. */
export function cohortHref(href: string, which: CohortName): string {
  return which === "shadow" ? href : `${href}?cohort=${which}`;
}

export type Opened = { shadow: Shadow } | { problem: string };

/** The configured source and its cohort, or why there is nothing to show. */
export async function openConfigured(which: CohortName = "shadow"): Promise<Opened> {
  const setting = shadowSource();
  if (!setting.source) return { problem: setting.reason };
  const shadow = await openShadow(setting.source, which);
  if (!shadow.cohort) {
    return {
      problem: `${COHORTS[which].label} has written nothing yet: no cohort ${shadow.cohortId} under ` +
        `${shadow.prefix} in ${setting.source.description}`,
    };
  }
  return { shadow };
}

export function ShadowNotConfigured({ problem }: { problem: string }) {
  return (
    <div className="empty">
      <p style={{ marginTop: 0 }}>
        <strong>The Phase B shadow has nothing to show.</strong> {problem}.
      </p>
      <p>
        Set <code>SURGE_SHADOW_SOURCE</code> in the server environment: <code>blob</code> for the shadow&apos;s
        private Vercel Blob store (its token comes from the store&apos;s connection to the project, never from
        the repository), or <code>fixture</code> for the synthetic cohort the screens are built against.
      </p>
      <p style={{ marginBottom: 0 }}>
        The official Phase B is unaffected either way: it runs on the operator&apos;s PC from its frozen
        worktree.
      </p>
    </div>
  );
}

export function ShadowBanner({ shadow }: { shadow: Shadow }) {
  const synthetic = isSynthetic(shadow);
  return (
    <>
      <div className={synthetic ? "notice stop" : "notice"}>
        {synthetic ? (
          <>
            <strong>Synthetic fixture — not real data.</strong> Made-up securities, prices, disclosures and
            answers, run through the real Phase B code with fakes for every provider, as of{" "}
            <span className="mono">{fmtJst(shadow.now.toISOString())}</span>. It exists so these screens can be
            built and checked before the shadow runs.
          </>
        ) : (
          <>
            <strong>Parallel shadow, not the official system.</strong> The official Phase B runs on the
            operator&apos;s PC (Windows Task Scheduler, frozen worktree). This is its cloud copy, compared with
            it day by day; switching is the operator&apos;s decision.
          </>
        )}
      </div>
      <p className="lede" style={{ marginTop: -12, fontSize: 12.5 }}>
        cohort <span className="mono">{shadow.cohortId}</span> · read from {shadow.source.description} ·{" "}
        <span className="mono">{shadow.prefix}/</span>
      </p>
    </>
  );
}

export function ShadowProblem({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="notice stop">
      <strong>Could not read the shadow&apos;s artifacts.</strong> {message}
    </div>
  );
}

export function ProbabilityNote() {
  return (
    <p className="lede">
      Probabilities and confidences are <strong>Jev&apos;s own answers, as it gave them</strong>: uncalibrated,
      not SURGE&apos;s probabilities, and no decision is taken from them (no threshold is introduced in Phase B).
      Their calibration is what the reports measure after T+20.
    </p>
  );
}

// ----------------------------------------------------------------- formatting

export function fmtJst(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return String(iso);
  return `${new Date(at.getTime() + 9 * 3_600_000).toISOString().replace("T", " ").slice(0, 16)} JST`;
}

export function fmtUsd(value: number | string | null | undefined, digits = 4): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isNaN(n) ? String(value) : `$${n.toFixed(digits)}`;
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function fmtRatio(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtSigned(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const pct = value * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

export function shortHash(value: string | null | undefined, n = 12): string {
  return value ? `${value.slice(0, n)}…` : "—";
}

export function Bool({ value }: { value: boolean | null | undefined }) {
  if (value === null || value === undefined) return <span className="mono">—</span>;
  return value ? <span className="tag ok">yes</span> : <span className="mono">no</span>;
}

export function StatusTag({ status }: { status: string }) {
  const tone =
    status === "sent" || status === "outcomes_frozen" || status === "final" || status === "frozen"
      ? "ok"
      : status.includes("stop") || status === "error" || status === "cohort_refused"
        ? "stop"
        : status === "partial" || status === "locked" || status === "no_window"
          ? "warn"
          : undefined;
  return <span className={tone ? `tag ${tone}` : "tag"}>{status}</span>;
}

export function DayLink({ s0, to = "/cohort/predictions", which = "shadow" }:
  { s0: string; to?: string; which?: CohortName }) {
  return (
    <Link href={`${to}?s0=${s0}${which === "shadow" ? "" : `&cohort=${which}`}`} className="mono">
      {s0}
    </Link>
  );
}
