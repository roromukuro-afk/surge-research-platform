import Link from "next/link";
import { Empty } from "@/components/Chrome";
import {
  ProbabilityNote,
  ShadowBanner,
  ShadowNotConfigured,
  ShadowProblem,
  StatusTag,
  fmtNum,
  fmtUsd,
  openConfigured,
} from "@/components/ShadowChrome";
import { type Prediction, type Sample, loadDays, loadPredictions, loadSamples, sentDays, cohortName } from "@/lib/shadow/model";

export const dynamic = "force-dynamic";

const SUBGROUP_LABEL: Record<string, string> = { D_ONLY: "D only", D_PLUS_OTHER: "D + other", NON_D: "non-D" };

function chosenProbability(row: Prediction): number | null {
  if (!row.decision || !row.decision_probabilities) return null;
  return row.decision_probabilities[row.decision] ?? null;
}

export default async function Predictions({ searchParams }: { searchParams: Promise<{ s0?: string; cohort?: string }> }) {
  const { cohort: chosen } = await searchParams;
  const opened = await openConfigured(cohortName(chosen));
  if ("problem" in opened) return <ShadowNotConfigured problem={opened.problem} />;
  const { shadow } = opened;
  const { s0: requested } = await searchParams;

  let rows: { prediction: Prediction; sample: Sample | undefined }[] = [];
  let available: string[] = [];
  let shown: string | undefined;
  try {
    const days = sentDays(await loadDays(shadow));
    available = days.map((d) => d.s0);
    shown = requested && available.includes(requested) ? requested : available[available.length - 1];
    const day = days.find((d) => d.s0 === shown);
    if (day) {
      const [predictions, samples] = await Promise.all([loadPredictions(shadow, day), loadSamples(shadow, day)]);
      const bySample = new Map(samples.map((s) => [s.sample_id, s]));
      rows = predictions.map((prediction) => ({ prediction, sample: bySample.get(prediction.sample_id) }));
    }
  } catch (error) {
    return (
      <>
        <ShadowBanner shadow={shadow} />
        <ShadowProblem error={error} />
      </>
    );
  }

  const answered = rows.filter((r) => r.prediction.status === "ok").length;
  const repeats = rows.filter((r) => r.prediction.variant !== "main").length;
  const cost = rows.reduce((n, r) => n + (r.prediction.cost_usd ?? 0), 0);

  return (
    <>
      <ShadowBanner shadow={shadow} />
      <h2>Predictions{shown ? ` — S0 ${shown}` : ""}</h2>
      <p className="lede">
        Every request of the day as it was answered: the Primary sample (passing the screener), the matched
        Control, and the anonymized and drift repeats. Route D&apos;s subgroup is recorded for every Primary
        sample and reported separately; it changes nothing about how a sample is drawn.
      </p>
      <ProbabilityNote />
      {available.length > 1 ? (
        <p className="lede">
          Day:{" "}
          {available.map((day, i) => (
            <span key={day}>
              {i ? " · " : ""}
              {day === shown ? <strong className="mono">{day}</strong> : <Link href={`/cohort/predictions?s0=${day}&cohort=${shadow.which}`} className="mono">{day}</Link>}
            </span>
          ))}
        </p>
      ) : null}

      {rows.length ? (
        <>
          <p className="lede">
            {rows.length} requests ({rows.length - repeats} Primary and Control, {repeats} anonymized or drift
            repeats) · {answered} answered · {fmtUsd(cost, 4)}
          </p>
          <table>
            <thead>
              <tr>
                <th>Code</th>
                <th>Company</th>
                <th>Cohort</th>
                <th>Variant</th>
                <th>Routes</th>
                <th>Route D</th>
                <th>Decision</th>
                <th className="num">P(decision)</th>
                <th className="num">Reaches target</th>
                <th className="num">Upside score</th>
                <th className="num">Confidence</th>
                <th>Model</th>
                <th className="num">Input tokens</th>
                <th className="num">Latency s</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ prediction: p, sample }) => (
                <tr key={`${p.sample_id}.${p.variant}`}>
                  <td className="mono">{p.code}</td>
                  <td>{sample?.name ?? "—"}</td>
                  <td>{p.cohort === "PRIMARY" ? "Primary" : "Control"}</td>
                  <td className="mono">{p.variant}</td>
                  <td className="mono">{sample?.routes.join(" ") || "—"}</td>
                  <td>{p.route_d_subgroup ? SUBGROUP_LABEL[p.route_d_subgroup] ?? p.route_d_subgroup : "—"}</td>
                  <td>{p.status === "ok" ? <span className="mono">{p.decision}</span> : <StatusTag status={p.status ?? "error"} />}</td>
                  <td className="num">{fmtNum(chosenProbability(p))}</td>
                  <td className="num">{fmtNum(p.reaches_target)}</td>
                  <td className="num">{fmtNum(p.upside_score)}</td>
                  <td className="num">{fmtNum(p.decision_confidence)}</td>
                  <td className="mono">{p.served_model ?? "—"}</td>
                  <td className="num">{fmtNum(p.input_tokens, 0)}</td>
                  <td className="num">{fmtNum(p.latency_seconds, 2)}</td>
                  <td className="num">{fmtUsd(p.cost_usd, 6)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <Empty what="predictions" why="A day's predictions appear once the day has been sent and committed." />
      )}
    </>
  );
}
