import { Empty, FailedToRead, Guard, Num, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import { ATTEMPT_LABELS, type EntryAttemptRow, type EntryAttemptStatus } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const TONE: Record<EntryAttemptStatus, "ok" | "warn" | "stop" | undefined> = {
  PREDICTION_CREATED: "ok",
  ENTRY_ABORTED_PRICE_LIMIT: "warn",
  REJECTED_HARD_FILTER_AT_DECISION: "stop",
  REJECTED_BY_ANALYSIS: "stop",
  REJECTED_NOT_IN_UNIVERSE: "stop",
  REAFFIRMED_EXISTING_EPISODE: undefined,
  NO_ENTRY_REFERENCE_PRICE: "warn",
};

export default async function Entries() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: EntryAttemptRow[];
  try {
    rows = await query<EntryAttemptRow>(
      `select * from ui.entry_attempt_ledger order by decision_completed_at desc limit 200`,
    );
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const produced = rows.filter((row) => row.produced_a_prediction).length;

  return (
    <>
      <h2>Entry decisions</h2>
      <p className="lede">
        Every intraday entry decision, including the ones that produced nothing. This is the
        denominator: a ledger showing only the entries that were taken would make any hit rate
        computed from it meaningless.
      </p>

      {rows.length > 0 ? (
        <div className="notice">
          <strong>
            {produced} of {rows.length} decisions produced a prediction.
          </strong>{" "}
          The rest were rejected by the analysis, refused by the universe rule, or stopped by the
          3,000 yen limit — which is checked twice, once against the price the model saw and again
          against the price that could actually have been paid.
        </div>
      ) : null}

      {rows.length === 0 ? (
        <Empty
          what="entry decisions"
          why="An entry decision runs during a session against a live price. No intraday provider is settled yet, so none has run."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th>Security</th>
              <th>Decided</th>
              <th className="num">Decision price (JPY)</th>
              <th className="num">Entry price (JPY)</th>
              <th>Universe</th>
              <th>Why not</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.attempt_id}>
                <td>
                  <Tag tone={TONE[row.status]}>{ATTEMPT_LABELS[row.status] ?? row.status}</Tag>
                </td>
                <td>
                  <code>{row.security_id.slice(0, 8)}</code>
                </td>
                <td>
                  <When value={row.decision_completed_at} />
                </td>
                <td className="num">
                  <Num value={row.decision_price_jpy} digits={0} />
                </td>
                <td className="num">
                  <Num value={row.entry_price_jpy} digits={0} />
                </td>
                <td>{row.universe_decision ?? "—"}</td>
                <td>{row.reject_reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
