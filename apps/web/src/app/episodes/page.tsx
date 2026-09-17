import { Empty, FailedToRead, Guard, Num, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type { OpenEpisodeRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

export default async function Episodes() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: OpenEpisodeRow[];
  try {
    rows = await query<OpenEpisodeRow>(
      `select * from ui.open_episodes order by opened_at desc limit 200`,
    );
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const notLiveVerified = rows.filter(
    (row) => row.verification === "IMPLEMENTED_NOT_LIVE_VERIFIED",
  ).length;

  return (
    <>
      <h2>Open episodes</h2>
      <p className="lede">
        One episode per security per thesis, from the first entry to the close. Scoring counts
        episodes, not predictions: a re-entry on the same thesis is the same claim stated twice, and
        it is recorded as a reaffirmation rather than as a second prediction.
      </p>

      {notLiveVerified > 0 ? (
        <div className="notice">
          <strong>{notLiveVerified} of these are marked not live-verified.</strong> No intraday price
          provider is settled yet, so the machinery has never run against a real live price. The rows
          exist because the pipeline was exercised, not because a trade was possible.
        </div>
      ) : null}

      {rows.length === 0 ? (
        <Empty
          what="open episodes"
          why="An episode opens only when an intraday decision produced a prediction, which needs a live price. The state machine, the guards and the storage are in place."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Security</th>
              <th>Thesis</th>
              <th>Opened</th>
              <th className="num">Entry</th>
              <th className="num">Target (+20%)</th>
              <th className="num">Initial failure line</th>
              <th className="num">Current risk line</th>
              <th className="num">Transitions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.episode_id}>
                <td>
                  <code>{row.security_id.slice(0, 8)}</code>
                </td>
                <td>{row.thesis_key}</td>
                <td>
                  <When value={row.entry_price_observed_at} />
                </td>
                <td className="num">
                  <Num value={row.entry_reference_price} /> {row.entry_price_currency}
                </td>
                <td className="num">
                  <Num value={row.target_price} />
                </td>
                <td className="num">
                  <Tag tone="stop">
                    <Num value={row.initial_failure_line} />
                  </Tag>
                </td>
                <td className="num">
                  <Num value={row.current_risk_line} />
                </td>
                <td className="num">{row.transition_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="lede">
        Both failure lines are shown because they answer different questions. The{" "}
        <strong>initial failure line</strong> is fixed when the prediction is made and is what the
        outcome is judged against. The <strong>current risk line</strong> may move on reanalysis and
        is research only — touching it never closes an episode, which is exactly what keeps the fixed
        line from becoming decorative.
      </p>
    </>
  );
}
