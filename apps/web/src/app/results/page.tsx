import { Empty, FailedToRead, Guard, Num, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import {
  OUTCOME_LABELS,
  type EpisodeResultRow,
  type OutcomeCountRow,
  type PrimaryOutcome,
} from "@/lib/contracts";

export const dynamic = "force-dynamic";

const TONE: Record<PrimaryOutcome, "ok" | "warn" | "stop" | undefined> = {
  TARGET_HIT: "ok",
  INITIAL_FAILURE_HIT: "stop",
  THESIS_INVALIDATED: "stop",
  HORIZON_EXPIRED: "warn",
  AMBIGUOUS_PATH: undefined,
  UNRESOLVED_MISSING_DATA: undefined,
  CORPORATE_ACTION_SUSPECTED: undefined,
};

const UNRESOLVED: PrimaryOutcome[] = [
  "AMBIGUOUS_PATH",
  "UNRESOLVED_MISSING_DATA",
  "CORPORATE_ACTION_SUSPECTED",
];

function Percent({ value }: { value: string | null }) {
  if (value === null) return <>—</>;
  return <>{(Number(value) * 100).toFixed(1)}%</>;
}

export default async function Results() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: EpisodeResultRow[];
  let counts: OutcomeCountRow[];
  try {
    [rows, counts] = await Promise.all([
      query<EpisodeResultRow>(
        `select * from ui.episode_results order by closed_at desc nulls last limit 200`,
      ),
      query<OutcomeCountRow>(`select * from ui.outcome_counts order by outcome`),
    ]);
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const unresolved = counts
    .filter((row) => UNRESOLVED.includes(row.outcome))
    .reduce((total, row) => total + Number(row.episodes), 0);
  const total = counts.reduce((sum, row) => sum + Number(row.episodes), 0);

  return (
    <>
      <h2>Results</h2>
      <p className="lede">
        One row per finished episode, with both layers side by side. The{" "}
        <strong>primary outcome</strong> answers whether the prediction was right. The{" "}
        <strong>counterfactual</strong> columns say what the price did anyway — a target reached
        after a thesis was invalidated belongs there and never becomes a success.
      </p>

      {unresolved > 0 ? (
        <div className="notice">
          <strong>
            {unresolved} of {total} episodes could not be resolved.
          </strong>{" "}
          A session that touched both the target and the failure line has two plausible answers.
          These are counted on their own rather than folded into either, because &ldquo;we could not
          tell&rdquo; is not a result.
        </div>
      ) : null}

      {counts.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th className="num">Episodes</th>
              <th className="num">Later reached target</th>
              <th className="num">Best excursion</th>
              <th className="num">Worst excursion</th>
            </tr>
          </thead>
          <tbody>
            {counts.map((row) => (
              <tr key={row.outcome}>
                <td>
                  <Tag tone={TONE[row.outcome]}>{OUTCOME_LABELS[row.outcome] ?? row.outcome}</Tag>
                </td>
                <td className="num">{row.episodes}</td>
                <td className="num">{row.later_reached_target}</td>
                <td className="num">
                  <Percent value={row.best_excursion} />
                </td>
                <td className="num">
                  <Percent value={row.worst_excursion} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {rows.length === 0 ? (
        <Empty
          what="finished episodes"
          why="An episode closes only after an entry that needs a live price. The engine, the ladder and the storage are all in place and exercised against fixtures."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th>Security</th>
              <th>Entered</th>
              <th className="num">Entry</th>
              <th className="num">Target</th>
              <th className="num">Failure</th>
              <th className="num">MFE</th>
              <th className="num">MAE</th>
              <th>Resolved by</th>
              <th>Counterfactual</th>
              <th>Corporate actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.episode_id}>
                <td>
                  <Tag tone={TONE[row.primary_outcome]}>
                    {OUTCOME_LABELS[row.primary_outcome] ?? row.primary_outcome}
                  </Tag>
                  {row.resolution_detail ? (
                    <div className="lede">{row.resolution_detail}</div>
                  ) : null}
                </td>
                <td>
                  <code>{row.security_id.slice(0, 8)}</code>
                </td>
                <td>
                  <When value={row.entry_price_observed_at} />
                </td>
                <td className="num">
                  <Num value={row.entry_reference_price} /> {row.outcome_currency}
                </td>
                <td className="num">
                  <Num value={row.target_price} />
                </td>
                <td className="num">
                  <Num value={row.initial_failure_line} />
                </td>
                <td className="num">
                  <Percent value={row.mfe} />
                </td>
                <td className="num">
                  <Percent value={row.mae} />
                </td>
                <td>
                  {row.resolution_granularity ?? "—"}
                  {row.resolved_session_index !== null ? ` · S${row.resolved_session_index}` : ""}
                </td>
                <td>
                  {row.later_target_hit
                    ? `reached target${
                        row.later_target_hit_session_index !== null
                          ? ` at S${row.later_target_hit_session_index}`
                          : ""
                      }`
                    : "—"}
                </td>
                <td>
                  {row.corporate_action_ids_applied.length > 0
                    ? row.corporate_action_ids_applied.join(", ")
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="lede">
        Outcomes are computed in the security&rsquo;s own currency. The yen value of an entry decides
        eligibility and nothing else, so a move that was +17% in dollars cannot become a +20% success
        because the exchange rate moved.
      </p>
    </>
  );
}
