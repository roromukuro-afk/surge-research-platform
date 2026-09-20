/** Shared pieces of the operational screens: the live system, not a cohort's artifacts (D-279). */

import {
  type CloudStatus,
  type PcStatus,
  type Published,
  newestStatus,
  statusHistory,
} from "@/lib/ops/status";
import { type ArtifactSource, shadowSource } from "@/lib/shadow/source";

export interface Ops {
  source: ArtifactSource;
  cloud: Published<CloudStatus> | null;
  pc: Published<PcStatus> | null;
  cloudHistory: Published<CloudStatus>[];
  now: Date;
}

export type OpsOpened = { ops: Ops } | { problem: string };

/** Everything the operational screens read, in one pass. */
export async function openOps(history = 7): Promise<OpsOpened> {
  const setting = shadowSource();
  if (!setting.source) return { problem: setting.reason };
  const source = setting.source;
  const now = new Date();
  const [cloudHistory, pc] = await Promise.all([
    statusHistory<CloudStatus>(source, "cloud", history, now),
    newestStatus<PcStatus>(source, "pc", now),
  ]);
  const cloud = cloudHistory.length ? cloudHistory[0] : await newestStatus<CloudStatus>(source, "cloud", now);
  return { ops: { source, cloud, pc, cloudHistory, now } };
}

export function OpsNotConfigured({ problem }: { problem: string }) {
  return (
    <div className="empty">
      <p style={{ marginTop: 0 }}>
        <strong>These screens have no store to read.</strong> {problem}.
      </p>
      <p style={{ marginBottom: 0 }}>
        Set <code>SURGE_SHADOW_SOURCE=blob</code> in the server environment. The official Phase B is
        unaffected either way: it runs on the operator&apos;s PC from its frozen worktree.
      </p>
    </div>
  );
}

/** Where a number came from and when, so nothing on these screens looks fresher than it is. */
export function PublishedAt({ what, at, by }: { what: string; at: string | null | undefined; by?: string }) {
  return (
    <p className="hint" style={{ margin: "6px 0 14px" }}>
      {what}
      {at ? <> published {fmtJstAgo(at)}</> : <> — nothing published yet</>}
      {by ? <> by {by}</> : null}
    </p>
  );
}

export function fmtJstAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return String(iso);
  const minutes = Math.round((now.getTime() - at.getTime()) / 60_000);
  const ago =
    minutes < 1 ? "just now"
    : minutes < 60 ? `${minutes} min ago`
    : minutes < 60 * 36 ? `${Math.round(minutes / 60)} h ago`
    : `${Math.round(minutes / 1440)} days ago`;
  return `${new Date(at.getTime() + 9 * 3_600_000).toISOString().replace("T", " ").slice(0, 16)} JST (${ago})`;
}

export function Row({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <tr>
      <th style={{ width: 280, textTransform: "none", letterSpacing: 0, fontSize: 13 }}>
        {label}
        {hint ? <div className="hint" style={{ fontWeight: 400 }}>{hint}</div> : null}
      </th>
      <td>{children}</td>
    </tr>
  );
}

export function Yes({ value, yes = "yes", no = "no" }: { value: boolean | null | undefined; yes?: string; no?: string }) {
  if (value === null || value === undefined) return <span className="mono">unknown</span>;
  return value ? <span className="tag ok">{yes}</span> : <span className="tag stop">{no}</span>;
}

export function Missing({ what }: { what: string }) {
  return (
    <div className="empty">
      <strong>Nothing published for {what} yet.</strong>
      <p style={{ margin: "6px 0 0" }}>
        The cloud writes its own record on every no-op (the daily cron runs one), and the operator&apos;s
        machine publishes the rest with <code>publish_status.py</code>.
      </p>
    </div>
  );
}
