import { Empty } from "@/components/Chrome";
import {
  DayLink,
  ShadowBanner,
  ShadowNotConfigured,
  ShadowProblem,
  StatusTag,
  fmtJst,
  fmtRatio,
  fmtUsd,
  openConfigured,
  shortHash,
} from "@/components/ShadowChrome";
import { type Day, type RunRecord, loadDays, loadRuns, stopReason } from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

function minutes(from?: string, to?: string): string {
  if (!from || !to) return "—";
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isNaN(ms) ? "—" : `${(ms / 60_000).toFixed(1)}`;
}

function brief(record: RunRecord): string {
  if (record.closed) return `closed: ${record.closed}`;
  if (record.status === "outcomes_frozen" || record.status === "no_targets") {
    const frozen = Object.entries(record.outcomes ?? {})
      .filter(([, r]) => r.status === "frozen")
      .map(([s0]) => s0);
    const report = record.report?.file ? ` · ${record.report.file} (${record.report.status})` : "";
    return frozen.length ? `froze ${frozen.join(", ")}${report}` : "nothing due";
  }
  const detail = record.detail;
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if ("requests_answered" in d) return `${d.requests_answered} answered · ${fmtUsd(d.spent_usd as string, 4)}`;
    return JSON.stringify(detail).slice(0, 160);
  }
  return detail ? String(detail).slice(0, 200) : "";
}

export default async function Runs() {
  const opened = await openConfigured();
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;

  let days: Day[];
  let runs: RunRecord[];
  try {
    [days, runs] = await Promise.all([loadDays(shadow), loadRuns(shadow)]);
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Business days</h2>
      <p className="lede">
        What each day did, from the universe down to the answers: securities in JPX&apos;s list and read from
        Yahoo, those at or under ¥3,000 as traded, those passing the screener (Routes A–H), the sample drawn,
        and the requests sent to TypeSafe.
      </p>
      {days.length ? (
        <table>
          <thead>
            <tr>
              <th>S0</th>
              <th>Status</th>
              <th className="num">Universe</th>
              <th className="num">Read</th>
              <th className="num">≤ ¥3,000</th>
              <th className="num">Screener pass</th>
              <th className="num">Primary</th>
              <th className="num">Control</th>
              <th className="num">Requests</th>
              <th className="num">Answered</th>
              <th className="num">Failed</th>
              <th className="num">Cost</th>
              <th>Started</th>
              <th>Completed</th>
              <th>Stop reason</th>
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map((day) => {
              const plan = day.run?.stages.plan;
              const run = day.run?.stages.run;
              const sent = run?.sent_this_time;
              const answered = run?.requests_answered;
              // The scheduler's record of the run that decided the day: when it started and ended.
              const decided = runs.find((r) => r.job === "prediction" && r.s0 === day.s0 && r.status !== "already_sent");
              return (
                <tr key={day.s0}>
                  <td>
                    <DayLink s0={day.s0} />
                  </td>
                  <td>
                    <StatusTag status={day.status} />
                  </td>
                  <td className="num">{plan?.issues ?? "—"}</td>
                  <td className="num" title={plan?.coverage !== undefined ? `coverage ${fmtRatio(plan.coverage)}` : undefined}>
                    {plan?.histories_read ?? "—"}
                  </td>
                  <td className="num">{plan?.eligible ?? "—"}</td>
                  <td className="num">{plan?.passing ?? "—"}</td>
                  <td className="num">{plan?.selected?.primary ?? "—"}</td>
                  <td className="num">{plan?.selected?.control ?? "—"}</td>
                  <td className="num">{plan?.requests ?? "—"}</td>
                  <td className="num">{answered ?? "—"}</td>
                  <td className="num">{sent !== undefined && answered !== undefined ? sent - answered : "—"}</td>
                  <td className="num">{fmtUsd(run?.spent_usd, 4)}</td>
                  <td className="mono">{fmtJst(decided?.started_at ?? day.run?.run_started[0]?.started_at)}</td>
                  <td className="mono">{fmtJst(decided?.finished_at ?? run?.finished_at)}</td>
                  <td style={{ maxWidth: 280 }}>{stopReason(day) ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <Empty what="business days" />
      )}

      <h2>Scheduled invocations</h2>
      <p className="lede">
        Every start of the prediction job (weekdays 16:10 JST) and the outcome job (weekdays 18:00 JST),
        whatever it found to do: a closed day or a day already sent is a normal, empty run.
      </p>
      {runs.length ? (
        <table>
          <thead>
            <tr>
              <th>Started</th>
              <th>Job</th>
              <th>Status</th>
              <th className="num">Exit</th>
              <th>S0</th>
              <th className="num">Minutes</th>
              <th>Detail</th>
              <th>Code</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((record) => (
              <tr key={record.key}>
                <td className="mono">{fmtJst(record.started_at)}</td>
                <td>{record.job}</td>
                <td>
                  <StatusTag status={record.status} />
                </td>
                <td className="num">{record.exit_code}</td>
                <td className="mono">{record.s0 ?? record.today ?? ""}</td>
                <td className="num">{minutes(record.started_at, record.finished_at)}</td>
                <td style={{ maxWidth: 360 }}>{brief(record)}</td>
                <td className="mono">{shortHash(record.code?.head, 8)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="scheduled runs" />
      )}
    </>
  );
}
