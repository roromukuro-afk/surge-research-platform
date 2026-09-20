import { Empty, Stat } from "@/components/Chrome";
import {
  ShadowBanner,
  ShadowNotConfigured,
  ShadowProblem,
  StatusTag,
  fmtJst,
  fmtNum,
  fmtRatio,
  fmtSigned,
  openConfigured,
} from "@/components/ShadowChrome";
import { type Report, loadReports, cohortName } from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

type Hit = {
  positives?: number;
  base_rate?: number | null;
  hit_rate?: number | null;
  brier?: number | null;
  brier_skill?: number | null;
  pr_auc_average_precision?: number | null;
  calibration?: { expected_calibration_error?: number | null };
  reaches_target_calibration?: { expected_calibration_error?: number | null };
};
type Subgroup = {
  n: number;
  hit_20_high: Hit;
  hit_20_close: Hit;
  mean_reaches_target: number | null;
  upside_score: { mean: number | null; median: number | null };
  max_upside: { high_median: number | null; close_median: number | null };
  max_drawdown: { low_median: number | null; close_median: number | null };
};

const SUBGROUPS: [string, string][] = [
  ["D_ONLY", "D only"],
  ["D_PLUS_OTHER", "D + other"],
  ["NON_D", "non-D"],
];

function ece(hit: Hit | undefined): number | null {
  return hit?.calibration?.expected_calibration_error ?? hit?.reaches_target_calibration?.expected_calibration_error ?? null;
}

export default async function Reports({ searchParams }: { searchParams: Promise<{ cohort?: string }> }) {
  const { cohort: chosen } = await searchParams;
  const opened = await openConfigured(cohortName(chosen));
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;

  let reports: Report[];
  try {
    reports = await loadReports(shadow);
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }
  const latest = reports[reports.length - 1];
  const primary = latest?.primary as { n: number; hit_20_high: Hit; hit_20_close: Hit } | undefined;
  const subgroups = latest?.route_d_subgroups as Record<string, Subgroup> | undefined;
  const progress = latest?.progress;

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Reports</h2>
      <p className="lede">
        Written by the outcome job when the frozen outcomes change: <strong>partial</strong> while any business
        day or outcome is outstanding, <strong>final</strong> once, when every one is in. Metrics read only
        predictions whose outcome is resolved; unresolved ones are counted, never failed, and in no denominator.
      </p>

      {latest && progress ? (
        <>
          <div className="cards">
            <Stat label="Latest" value={<StatusTag status={latest.status} />} hint={`${latest.file} · ${fmtJst(latest.cohort.reported_at)}`} />
            <Stat
              label="Resolved"
              value={progress.predictions.resolved.total}
              hint={`Primary ${progress.predictions.resolved.PRIMARY} · Control ${progress.predictions.resolved.CONTROL}`}
            />
            <Stat
              label="Unresolved"
              value={progress.predictions.unresolved.total.total}
              hint={`awaiting T+20 ${progress.predictions.unresolved.awaiting_t_plus_20.total} · missing data ${progress.predictions.unresolved.missing_data.total}`}
            />
            <Stat
              label="Cohort completion"
              value={fmtRatio(progress.cohort_completion_rate)}
              hint={`${progress.days_with_outcomes} of ${progress.target_business_days} days with outcomes`}
            />
          </div>

          {primary ? (
            <>
              <h2>Primary — Jev&apos;s reaches-target answer against the outcome</h2>
              <p className="lede">
                n = {primary.n} resolved Primary predictions. Brier and calibration error score Jev&apos;s own
                probability; skill is against always answering the base rate. Undefined values show as —.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Target</th>
                    <th className="num">Hits</th>
                    <th className="num">Base rate</th>
                    <th className="num">Brier</th>
                    <th className="num">Brier skill</th>
                    <th className="num">Calibration error</th>
                    <th className="num">PR-AUC</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ["+20% on the high", primary.hit_20_high],
                      ["+20% on the close", primary.hit_20_close],
                    ] as [string, Hit][]
                  ).map(([label, hit]) => (
                    <tr key={label}>
                      <td>{label}</td>
                      <td className="num">{hit.positives ?? "—"}</td>
                      <td className="num">{fmtRatio(hit.base_rate)}</td>
                      <td className="num">{fmtNum(hit.brier, 4)}</td>
                      <td className="num">{fmtNum(hit.brier_skill, 3)}</td>
                      <td className="num">{fmtNum(ece(hit), 4)}</td>
                      <td className="num">{fmtNum(hit.pr_auc_average_precision, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : null}

          {subgroups ? (
            <>
              <h2>Route D subgroups (pre-registered)</h2>
              <table>
                <thead>
                  <tr>
                    <th>Subgroup</th>
                    <th className="num">n</th>
                    <th className="num">Hit rate, high</th>
                    <th className="num">Hit rate, close</th>
                    <th className="num">Brier, high</th>
                    <th className="num">Mean reaches target</th>
                    <th className="num">Upside score, median</th>
                    <th className="num">Max upside, median</th>
                    <th className="num">Max drawdown, median</th>
                  </tr>
                </thead>
                <tbody>
                  {SUBGROUPS.map(([key, label]) => {
                    const g = subgroups[key];
                    if (!g) return null;
                    return (
                      <tr key={key}>
                        <td>{label}</td>
                        <td className="num">{g.n}</td>
                        <td className="num">{fmtRatio(g.hit_20_high?.hit_rate)}</td>
                        <td className="num">{fmtRatio(g.hit_20_close?.hit_rate)}</td>
                        <td className="num">{fmtNum(g.hit_20_high?.brier, 4)}</td>
                        <td className="num">{fmtNum(g.mean_reaches_target, 3)}</td>
                        <td className="num">{fmtNum(g.upside_score?.median, 2)}</td>
                        <td className="num">{fmtSigned(g.max_upside?.high_median)}</td>
                        <td className="num">{fmtSigned(g.max_drawdown?.low_median)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </>
          ) : null}

          <h2>Every report</h2>
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th>Reported</th>
                <th className="num">Days sent</th>
                <th className="num">With outcomes</th>
                <th className="num">Resolved</th>
                <th className="num">Unresolved</th>
              </tr>
            </thead>
            <tbody>
              {[...reports].reverse().map((report) => (
                <tr key={report.file}>
                  <td className="mono">{report.file}</td>
                  <td>
                    <StatusTag status={report.status} />
                  </td>
                  <td className="mono">{fmtJst(report.cohort.reported_at)}</td>
                  <td className="num">{report.progress.days_sent_in_full}</td>
                  <td className="num">{report.progress.days_with_outcomes}</td>
                  <td className="num">{report.progress.predictions.resolved.total}</td>
                  <td className="num">{report.progress.predictions.unresolved.total.total}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <details style={{ marginTop: 18 }}>
            <summary className="mono">{latest.file}, as written</summary>
            <pre className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 520, overflow: "auto" }}>
              {JSON.stringify(latest, null, 2)}
            </pre>
          </details>
        </>
      ) : (
        <Empty what="reports" why="The first report is written when the first day's T+20 outcomes are frozen." />
      )}
    </>
  );
}
