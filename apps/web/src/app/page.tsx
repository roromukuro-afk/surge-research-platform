import Link from "next/link";
import { Empty, Stat } from "@/components/Chrome";
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
import {
  type Day,
  type Shadow,
  businessDayAfter,
  closeConfirmedAt,
  cohortSpend,
  isBusinessDay,
  jstDate,
  addDays,
  loadDays,
  loadOutcomes,
  loadReports,
  loadRuns,
  loadStops,
  nextWeekdayAt,
  sentDays,
  stopReason,
} from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

function nextBusinessS0(shadow: Shadow, from: Date): string | null {
  const { calendar, cohort } = shadow;
  if (!calendar || !cohort) return null;
  let day = jstDate(from) < cohort.prospective_start ? cohort.prospective_start : jstDate(from);
  for (let i = 0; i < 30; i++, day = addDays(day, 1)) {
    const business = isBusinessDay(calendar, day);
    if (business === null) return null;
    if (business && closeConfirmedAt(day) > from) return day;
  }
  return null;
}

export default async function Overview() {
  const opened = await openConfigured();
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;
  const cohort = shadow.cohort!;

  let data;
  try {
    const [days, runs, reports, stops] = await Promise.all([
      loadDays(shadow),
      loadRuns(shadow),
      loadReports(shadow),
      loadStops(shadow),
    ]);
    const outcomes = await Promise.all(days.map(async (day) => [day.s0, await loadOutcomes(shadow, day)] as const));
    data = { days, runs, reports, stops, outcomes: new Map(outcomes) };
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }
  const { days, runs, reports, stops, outcomes } = data;
  const sent = sentDays(days);
  const target = cohort.target_business_days;
  const spent = cohortSpend(days);
  const cap = Number(cohort.budget.global_hard_cap_usd);
  const latestReport = reports[reports.length - 1];
  const final = reports.some((r) => r.status === "final");

  const predictionsSent = sent.reduce(
    (n, day) => n + (day.run?.stages.plan?.selected ? day.run.stages.plan.selected.primary + day.run.stages.plan.selected.control : 0),
    0,
  );
  let resolved = 0;
  let missingData = 0;
  for (const day of sent) {
    for (const row of outcomes.get(day.s0) ?? []) {
      if (row.resolution === "RESOLVED") resolved++;
      else missingData++;
    }
  }
  const unresolved = predictionsSent - resolved;

  const status = stops.length
    ? "stopped"
    : final
      ? "final"
      : sent.length >= target
        ? "awaiting outcomes"
        : jstDate(shadow.now) < cohort.prospective_start
          ? "not started"
          : "collecting";
  const statusHint =
    status === "awaiting outcomes" ? `all ${target} business days collected` : `first S0 ${cohort.prospective_start}`;
  const latestRun = runs[0];
  const nextPrediction = nextWeekdayAt(shadow.now, 16, 10);
  const nextOutcome = nextWeekdayAt(shadow.now, 18, 0);
  const nextS0 = sent.length >= target ? null : nextBusinessS0(shadow, shadow.now);

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Phase B</h2>
      <p className="lede">
        Jev ({cohort.requested_model}, pinned) answers a daily sample of TSE securities after the close; the
        answers are judged against prices after T+20. Nothing here reaches production predictions or the
        teacher data.
      </p>

      {stops.length ? (
        <div className="notice stop">
          <strong>The cohort is stopped.</strong> {String(stops[stops.length - 1].reason ?? "")} — no further
          day runs in it.
        </div>
      ) : null}

      <div className="cards">
        <Stat label="Status" value={<StatusTag status={status} />} hint={statusHint} />
        <Stat
          label="Business days"
          value={`${sent.length} / ${target}`}
          hint={`${days.length - sent.length} stopped or incomplete`}
        />
        <Stat
          label="TypeSafe spend"
          value={fmtUsd(spent, 3)}
          hint={`of the ${fmtUsd(cap, 2)} cohort cap (${fmtRatio(cap ? spent / cap : null)})`}
        />
        <Stat label="Outcomes resolved" value={resolved} hint={`of ${predictionsSent} predictions sent`} />
        <Stat
          label="Unresolved"
          value={unresolved}
          hint={`${unresolved - missingData} awaiting T+20 · ${missingData} missing data`}
        />
        <Stat label="Model" value={<span style={{ fontSize: 16 }}>{cohort.pinned_served_model}</span>} hint={cohort.provider} />
      </div>

      <h2>Runs</h2>
      <div className="cards">
        <Stat
          label="Latest run"
          value={latestRun ? <StatusTag status={latestRun.status} /> : "—"}
          hint={latestRun ? `${latestRun.job} · ${fmtJst(latestRun.started_at)}` : "no run recorded yet"}
        />
        <Stat
          label="Next prediction run"
          value={<span style={{ fontSize: 15 }}>{fmtJst(nextPrediction.toISOString())}</span>}
          hint={nextS0 ? `next S0 ${nextS0}` : sent.length >= target ? "all business days collected" : "beyond the calendar"}
        />
        <Stat
          label="Next outcome run"
          value={<span style={{ fontSize: 15 }}>{fmtJst(nextOutcome.toISOString())}</span>}
          hint="freezes each day's T+20 once it has closed"
        />
        <Stat
          label="Latest report"
          value={latestReport ? <StatusTag status={latestReport.status} /> : "—"}
          hint={
            latestReport
              ? `${latestReport.file} · completion ${fmtRatio(latestReport.progress.cohort_completion_rate)}`
              : "none before the first outcome"
          }
        />
      </div>

      <h2>Business days</h2>
      {days.length ? (
        <table>
          <thead>
            <tr>
              <th>S0</th>
              <th>Status</th>
              <th className="num">Read</th>
              <th className="num">Passing</th>
              <th className="num">Primary</th>
              <th className="num">Control</th>
              <th className="num">Answered</th>
              <th className="num">Spent</th>
              <th>Outcomes</th>
              <th>System</th>
            </tr>
          </thead>
          <tbody>
            {[...days].reverse().map((day: Day) => {
              const plan = day.run?.stages.plan;
              const run = day.run?.stages.run;
              const due = shadow.calendar ? businessDayAfter(shadow.calendar, day.s0, 20) : null;
              const rows = outcomes.get(day.s0) ?? [];
              return (
                <tr key={day.s0}>
                  <td>
                    <DayLink s0={day.s0} />
                  </td>
                  <td>
                    <StatusTag status={day.status} />
                    {day.status !== "sent" ? (
                      <div className="hint" style={{ fontSize: 12, color: "var(--ink-soft)", maxWidth: 320 }}>
                        {stopReason(day)}
                      </div>
                    ) : null}
                  </td>
                  <td className="num">{plan ? `${plan.histories_read}/${plan.issues}` : "—"}</td>
                  <td className="num">{plan?.passing ?? "—"}</td>
                  <td className="num">{plan?.selected?.primary ?? "—"}</td>
                  <td className="num">{plan?.selected?.control ?? "—"}</td>
                  <td className="num">{run ? `${run.requests_answered}/${run.sent_this_time}` : "—"}</td>
                  <td className="num">{fmtUsd(run?.spent_usd, 4)}</td>
                  <td>
                    {day.status !== "sent" ? (
                      "—"
                    ) : day.outcomeCommit ? (
                      <Link href={`/outcomes?s0=${day.s0}`}>{rows.filter((r) => r.resolution === "RESOLVED").length} resolved</Link>
                    ) : (
                      <span className="mono">T+20 {due ?? "?"}</span>
                    )}
                  </td>
                  <td>
                    <span className="tag">{day.system}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <Empty
          what="business days"
          why={`The first S0 is ${cohort.prospective_start}; a day appears once its integrity record is written.`}
        />
      )}

      <p className="lede" style={{ marginTop: 26 }}>
        Protocol fingerprint <span className="mono">{shortHash(cohort.frozen_fingerprint, 16)}</span> — the
        hashes behind it are on the <Link href="/system">system</Link> screen.
      </p>
    </>
  );
}
