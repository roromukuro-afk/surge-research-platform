import Link from "next/link";
import { Empty } from "@/components/Chrome";
import {
  Bool,
  ShadowBanner,
  ShadowNotConfigured,
  ShadowProblem,
  fmtSigned,
  openConfigured,
} from "@/components/ShadowChrome";
import {
  type CohortName,  type Day,
  type Outcome,
  type Sample,
  businessDayAfter,
  loadDays,
  loadOutcomes,
  loadSamples,
  sentDays,
  cohortName,
} from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

export default async function Outcomes({ searchParams }: { searchParams: Promise<{ s0?: string; cohort?: string }> }) {
  const { cohort: chosen } = await searchParams;
  const opened = await openConfigured(cohortName(chosen));
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;
  const { s0: requested } = await searchParams;

  let frozen: Day[] = [];
  let awaiting: { s0: string; due: string | null }[] = [];
  let shown: Day | undefined;
  let rows: { outcome: Outcome; sample: Sample | undefined }[] = [];
  try {
    const days = sentDays(await loadDays(shadow));
    frozen = days.filter((d) => d.outcomeCommit);
    awaiting = days
      .filter((d) => !d.outcomeCommit)
      .map((d) => ({ s0: d.s0, due: shadow.calendar ? businessDayAfter(shadow.calendar, d.s0, 20) : null }));
    shown = frozen.find((d) => d.s0 === requested) ?? frozen[frozen.length - 1];
    if (shown) {
      const [outcomes, samples] = await Promise.all([loadOutcomes(shadow, shown), loadSamples(shadow, shown)]);
      const bySample = new Map(samples.map((s) => [s.sample_id, s]));
      rows = outcomes.map((outcome) => ({ outcome, sample: bySample.get(outcome.sample_id) }));
    }
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }
  const unresolved = rows.filter((r) => r.outcome.resolution !== "RESOLVED").length;

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Outcomes{shown ? ` — S0 ${shown.s0}` : ""}</h2>
      <p className="lede">
        Frozen once, after T+20 has closed by JPX&apos;s calendar, from prices read then; Jev&apos;s answers are
        not read to compute them. Returns are from the S0 close as traded. The +20% target is shown on the high
        and on the close; neither is the label, and neither is merged with the success label (D-268).
      </p>
      {frozen.length > 1 ? (
        <p className="lede">
          Day:{" "}
          {frozen.map((day, i) => (
            <span key={day.s0}>
              {i ? " · " : ""}
              {day === shown ? <strong className="mono">{day.s0}</strong> : <Link href={`/cohort/outcomes?s0=${day.s0}&cohort=${shadow.which}`} className="mono">{day.s0}</Link>}
            </span>
          ))}
        </p>
      ) : null}

      {rows.length ? (
        <>
          <p className="lede">
            {rows.length} samples · {rows.length - unresolved} resolved · {unresolved} unresolved (a missing session;
            not a failure, and in no denominator)
          </p>
          <table>
            <thead>
              <tr>
                <th>Code</th>
                <th>Company</th>
                <th>Cohort</th>
                <th>Resolution</th>
                <th className="num">T+1</th>
                <th className="num">T+3</th>
                <th className="num">T+5</th>
                <th className="num">T+10</th>
                <th className="num">T+20</th>
                <th>+20% high</th>
                <th>+20% close</th>
                <th className="num">Max upside</th>
                <th className="num">Max drawdown</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ outcome: o, sample }) => (
                <tr key={o.sample_id}>
                  <td className="mono">{sample?.code ?? "—"}</td>
                  <td>{sample?.name ?? "—"}</td>
                  <td>{sample ? (sample.cohort === "PRIMARY" ? "Primary" : "Control") : "—"}</td>
                  <td>{o.resolution === "RESOLVED" ? <span className="mono">RESOLVED</span> : <span className="tag warn">{o.resolution}</span>}</td>
                  <td className="num">{fmtSigned(o.ret_t1)}</td>
                  <td className="num">{fmtSigned(o.ret_t3)}</td>
                  <td className="num">{fmtSigned(o.ret_t5)}</td>
                  <td className="num">{fmtSigned(o.ret_t10)}</td>
                  <td className="num">{fmtSigned(o.ret_t20)}</td>
                  <td>
                    <Bool value={o.hit_20_high} />
                  </td>
                  <td>
                    <Bool value={o.hit_20_close} />
                  </td>
                  <td className="num">{fmtSigned(o.max_upside_high)}</td>
                  <td className="num">{fmtSigned(o.max_drawdown_low)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <Empty what="frozen outcomes" why="A day's outcomes are frozen after its T+20 session has closed." />
      )}

      <h2>Awaiting T+20</h2>
      {awaiting.length ? (
        <table>
          <thead>
            <tr>
              <th>S0</th>
              <th>T+20 by the calendar</th>
            </tr>
          </thead>
          <tbody>
            {awaiting.map((day) => (
              <tr key={day.s0}>
                <td className="mono">{day.s0}</td>
                <td className="mono">{day.due ?? "beyond the calendar"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="days awaiting T+20" />
      )}
    </>
  );
}
