import { Empty } from "@/components/Chrome";
import {
  ShadowBanner,
  ShadowNotConfigured,
  ShadowProblem,
  StatusTag,
  fmtJst,
  fmtUsd,
  openConfigured,
} from "@/components/ShadowChrome";
import {
  type CreditReading,
  type Day,
  type RunRecord,
  addDays,
  isBusinessDay,
  jstDate,
  loadCredit,
  loadDays,
  loadRuns,
  loadStops,
  stopReason,
  cohortName,
} from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <tr>
      <th style={{ width: 260, textTransform: "none", letterSpacing: 0, fontSize: 13 }}>{label}</th>
      <td>{children}</td>
    </tr>
  );
}

export default async function System({ searchParams }: { searchParams: Promise<{ cohort?: string }> }) {
  const { cohort: chosen } = await searchParams;
  const opened = await openConfigured(cohortName(chosen));
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;
  const cohort = shadow.cohort!;
  const frozen = cohort.frozen;

  let days: Day[];
  let runs: RunRecord[];
  let credit: CreditReading[];
  let stops: Record<string, unknown>[];
  try {
    [days, runs, credit, stops] = await Promise.all([loadDays(shadow), loadRuns(shadow), loadCredit(shadow), loadStops(shadow)]);
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }
  const stoppedDays = days.filter((d) => d.status !== "sent");
  const failedRuns = runs.filter((r) => r.exit_code !== 0);
  const today = jstDate(shadow.now);
  const upcomingClosed = shadow.calendar
    ? Object.entries(shadow.calendar.closed_days).filter(([day]) => day >= today && day <= addDays(today, 60))
    : [];
  const typesafeKey = Boolean(process.env.TYPESAFE_API_KEY);

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Frozen protocol</h2>
      <p className="lede">
        What the cohort may not change while it runs. Hashes only: Canonical v5.1 and the addenda are never
        copied into an artifact or shown here. A day whose code differs from any of these is refused.
      </p>
      <table>
        <tbody>
          <Row label="Protocol fingerprint">
            <span className="mono">{cohort.frozen_fingerprint}</span>
          </Row>
          <Row label="Canonical v5.1">
            <span className="mono">
              {frozen.method.canonical.sha256} · {frozen.method.canonical.path}
            </span>
          </Row>
          <Row label="Addenda (newer overrides older)">
            {frozen.method.addenda_newer_overrides_older.map((a) => (
              <div key={a.path} className="mono">
                {a.sha256} · {a.path}
              </div>
            ))}
          </Row>
          <Row label="Question schema">
            <span className="mono">{frozen.question_schema_hash}</span>
          </Row>
          <Row label="Versions">
            <span className="mono">
              {frozen.evaluation_version} · {frozen.state_version} · {frozen.selection.version} ·{" "}
              {String(frozen.screener.route_version)} on {String(frozen.screener.feature_version)} · {frozen.outcome.version}
            </span>
          </Row>
          <Row label="Per day">
            <span className="mono">
              {Object.entries(frozen.selection.per_day)
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ")}
            </span>
          </Row>
          <Row label="Frozen files">
            {Object.entries(frozen.files_sha256).map(([file, sha]) => (
              <div key={file} className="mono">
                {sha ?? "missing"} · {file}
              </div>
            ))}
          </Row>
        </tbody>
      </table>

      <h2>Model and money</h2>
      <table>
        <tbody>
          <Row label="Jev">
            <span className="mono">
              {cohort.requested_model} requested, {cohort.pinned_served_model} required of every answer
            </span>
          </Row>
          <Row label="Provider">
            <span className="mono">{cohort.provider}</span> — {cohort.pacing}
          </Row>
          <Row label="Send window">{cohort.send_window}</Row>
          <Row label="Budget">
            cohort cap {fmtUsd(cohort.budget.global_hard_cap_usd, 2)} · per day {fmtUsd(cohort.budget.daily_budget_usd, 2)} and{" "}
            {cohort.budget.daily_max_requests} requests · {cohort.budget.credit}
          </Row>
          <Row label="API key">
            Never shown, logged or stored in an artifact. <span className="mono">TYPESAFE_API_KEY</span> on this
            deployment: {typesafeKey ? <span className="tag ok">configured</span> : <span className="tag">not configured</span>}
          </Row>
        </tbody>
      </table>

      <h2>TypeSafe credit readings</h2>
      <p className="lede">
        TypeSafe has no balance API: the operator reads the console and records it. Nothing buys credit; a new
        monthly credit is used only after its balance has been read and recorded.
      </p>
      {credit.length ? (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th className="num">Balance</th>
              <th>Confirmed</th>
              <th>Expires</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {credit.map((c) => (
              <tr key={c.n}>
                <td className="mono">{c.n}</td>
                <td className="num">{fmtUsd(c.confirmed_balance_usd, 2)}</td>
                <td className="mono">{fmtJst(c.confirmed_at)}</td>
                <td className="mono">{c.displayed_expiry ?? fmtJst(c.expires_at)}</td>
                <td>{c.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="credit readings" />
      )}

      <h2>Schedule</h2>
      <table>
        <tbody>
          <Row label="Prediction job">
            weekdays 16:10 JST (<span className="mono">10 7 * * 1-5</span> UTC) — one business day, from the
            universe to the answers, only inside its send window (until 09:00 JST on the next weekday)
          </Row>
          <Row label="Outcome job">
            weekdays 18:00 JST (<span className="mono">0 9 * * 1-5</span> UTC) — outcomes whose T+20 has closed,
            then the rolling or final report; never calls Jev
          </Row>
          <Row label="On Vercel Hobby">
            a cron fires within the hour it names, so the jobs wait for their window themselves; a closed day is a
            normal, empty run
          </Row>
          <Row label="Calendar">
            <span className="mono">{shadow.calendar?.version ?? "—"}</span> ({shadow.calendar?.years.join(", ")}) —{" "}
            {shadow.calendar?.source}
          </Row>
          <Row label="Closed in the next 60 days">
            {upcomingClosed.length
              ? upcomingClosed.map(([day, why]) => (
                  <span key={day} className="mono" style={{ marginRight: 14 }}>
                    {day} {why}
                  </span>
                ))
              : "none"}
            {shadow.calendar && isBusinessDay(shadow.calendar, addDays(today, 60)) === null ? " (the calendar ends before then)" : ""}
          </Row>
        </tbody>
      </table>

      <h2>Stops and errors</h2>
      {stops.length || stoppedDays.length || failedRuns.length ? (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>What</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {stops.map((stop, i) => (
              <tr key={`stop-${i}`}>
                <td className="mono">{fmtJst(String(stop.stopped_at ?? ""))}</td>
                <td>cohort</td>
                <td>
                  <StatusTag status="stopped" />
                </td>
                <td>{String(stop.reason ?? "")}</td>
              </tr>
            ))}
            {stoppedDays.map((day) => (
              <tr key={`day-${day.s0}`}>
                <td className="mono">{day.s0}</td>
                <td>business day</td>
                <td>
                  <StatusTag status={day.status} />
                </td>
                <td>{stopReason(day)}</td>
              </tr>
            ))}
            {failedRuns.map((run) => (
              <tr key={run.key}>
                <td className="mono">{fmtJst(run.started_at)}</td>
                <td>{run.job} job</td>
                <td>
                  <StatusTag status={run.status} />
                </td>
                <td>{typeof run.detail === "string" ? run.detail : JSON.stringify(run.detail ?? "").slice(0, 240)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="stops or errors" />
      )}
    </>
  );
}
