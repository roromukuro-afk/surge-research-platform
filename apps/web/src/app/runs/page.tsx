/**
 * Every run the cloud has done (D-279): the shadow's days, the rehearsal, the
 * probes that checked the read path, the no-ops the cron fires, and the
 * comparisons of the PC's day with the cloud's. One table, because an operator
 * asks the same questions of all of them.
 */

import Link from "next/link";
import { Empty } from "@/components/Chrome";
import { OpsNotConfigured, fmtJstAgo, openOps } from "@/components/OpsChrome";
import { type RunRow, loadRunRows } from "@/lib/ops/runs";
import { type CompareStatus, newestStatus } from "@/lib/ops/status";

export const dynamic = "force-dynamic";

const KIND_TONE: Record<string, string> = {
  "shadow day": "ok",
  rehearsal: "",
  probe: "",
  "no-op": "",
  compare: "warn",
};

function num(value: number | null, digits = 0): string {
  return value === null || value === undefined ? "—" : value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function Cell({ row }: { row: RunRow }) {
  return (
    <>
      {row.href ? <Link href={row.href}>{row.what}</Link> : row.what}
      {row.detail ? <div className="hint">{row.detail}</div> : null}
    </>
  );
}

export default async function Runs() {
  const opened = await openOps(14);
  if ("problem" in opened) return <OpsNotConfigured problem={opened.problem} />;
  const { ops } = opened;
  const compare = await newestStatus<CompareStatus>(ops.source, "compare", ops.now);
  const rows = await loadRunRows(ops.source, ops.cloudHistory, ops.pc?.record ?? null, compare);

  return (
    <>
      <section className="panel">
        <h2>Runs</h2>
        <p className="hint" style={{ marginTop: 0 }}>
          A <strong>shadow day</strong> is the cohort&apos;s day run in the cloud (from 2026-09-24); a{" "}
          <strong>rehearsal</strong> is the same pipeline on a past day under its own cohort; a{" "}
          <strong>probe</strong> checks the read path without writing anything; a <strong>no-op</strong> is what
          the daily cron runs, and it is how this deployment publishes what it is; a{" "}
          <strong>compare</strong> is the PC&apos;s day against the cloud&apos;s. No run here sends a request to
          a model.
        </p>
        {rows.length === 0 ? (
          <Empty what="runs" why="Nothing has been published or written yet." />
        ) : (
          <table>
            <thead>
              <tr>
                <th>Kind</th>
                <th>What</th>
                <th>Status</th>
                <th>Started</th>
                <th className="right">Elapsed</th>
                <th className="right">Universe</th>
                <th className="right">Coverage</th>
                <th className="right">Passing</th>
                <th className="right">Primary / Control</th>
                <th className="right">Events</th>
                <th className="right">CPU</th>
                <th className="right">Peak</th>
                <th>Stopped because</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.kind}-${row.what}-${i}`}>
                  <td>
                    <span className={KIND_TONE[row.kind] ? `tag ${KIND_TONE[row.kind]}` : "tag"}>{row.kind}</span>
                  </td>
                  <td><Cell row={row} /></td>
                  <td>{row.status}</td>
                  <td className="mono">{row.startedAt ? fmtJstAgo(row.startedAt, ops.now) : "—"}</td>
                  <td className="right mono">{row.seconds === null ? "—" : `${num(row.seconds)}s`}</td>
                  <td className="right mono">{num(row.universe)}</td>
                  <td className="right mono">
                    {row.coverage === null ? "—" : `${(row.coverage * 100).toFixed(1)}%`}
                  </td>
                  <td className="right mono">{num(row.passing)}</td>
                  <td className="right mono">
                    {row.primary === null && row.control === null ? "—" : `${num(row.primary)} / ${num(row.control)}`}
                  </td>
                  <td className="right mono">{num(row.events)}</td>
                  <td className="right mono">{row.cpuSeconds === null ? "—" : `${num(row.cpuSeconds, 1)}s`}</td>
                  <td className="right mono">{row.memoryMb === null ? "—" : `${num(row.memoryMb, 1)} MB`}</td>
                  <td className="hint">{row.stopReason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <p className="hint">
        A day&apos;s own artifacts are under its cohort:{" "}
        <Link href="/cohort?cohort=rehearsal">the rehearsal</Link>,{" "}
        <Link href="/cohort">the shadow</Link>, and the made-up <Link href="/demo">fixture</Link>.
      </p>
    </>
  );
}
