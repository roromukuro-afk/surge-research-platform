import { Empty, FailedToRead, Guard, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type {
  CalibrationRow,
  ModelVersionRow,
  PromotionAuditRow,
  WalkForwardRow,
} from "@/lib/contracts";

export const dynamic = "force-dynamic";

function Score({ value }: { value: string | null }) {
  // A missing score is shown as missing. Rendering it as 0.00 would make an
  // unmeasured fold look like a bad one.
  if (value === null) return <span className="lede">not measured</span>;
  return <>{Number(value).toFixed(4)}</>;
}

export default async function ModelLab() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let models: ModelVersionRow[];
  let folds: WalkForwardRow[];
  let calibration: CalibrationRow[];
  let promotions: PromotionAuditRow[];
  try {
    [models, folds, calibration, promotions] = await Promise.all([
      query<ModelVersionRow>(`select * from ui.model_version_comparison order by model_version`),
      query<WalkForwardRow>(
        `select * from ui.walk_forward_results order by model_version, fold_index limit 200`,
      ),
      query<CalibrationRow>(
        `select * from ui.calibration_diagnostics order by model_version, bin_lower limit 100`,
      ),
      query<PromotionAuditRow>(`select * from ui.promotion_audit order by attempted_at desc limit 50`),
    ]);
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const nothingYet =
    models.length === 0 && folds.length === 0 && calibration.length === 0 && promotions.length === 0;

  return (
    <>
      <h2>Model lab</h2>
      <p className="lede">
        Walk-forward evaluation, champion and challenger, and the record of every promotion that was
        refused. Nothing here is trained or promoted yet, and nothing is filled in to look like it
        is.
      </p>

      {nothingYet ? (
        <div className="notice">
          <strong>The model lab is empty, and that is the current state.</strong> There are no real
          episodes, so there are no real outcomes, so there are no teacher labels and nothing to
          train on. The frame exists so that what a promotion requires was decided before anybody
          wanted a particular answer — and the gate refuses on every condition today.
        </div>
      ) : null}

      <h3>Models</h3>
      {models.length === 0 ? (
        <Empty
          what="models"
          why="A model needs a teacher dataset, which needs outcomes, which need episodes that ran against real prices."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Version</th>
              <th>Trained</th>
              <th>Dataset</th>
              <th className="num">Labels</th>
              <th className="num">Folds</th>
              <th className="num">Mean score</th>
              <th className="num">Folds unmeasured</th>
              <th className="num">Calibration bins</th>
              <th>Live-verified labels</th>
            </tr>
          </thead>
          <tbody>
            {models.map((row) => (
              <tr key={row.model_version}>
                <td>
                  <code>{row.model_version}</code>
                </td>
                <td>
                  <When value={row.trained_at} />
                </td>
                <td>{row.dataset_name ?? "—"}</td>
                <td className="num">{row.labels_admitted ?? "—"}</td>
                <td className="num">{row.fold_count ?? "—"}</td>
                <td className="num">
                  <Score value={row.mean_score} />
                </td>
                <td className="num">{row.folds_without_a_score}</td>
                <td className="num">{row.calibration_bins}</td>
                <td>
                  <Tag tone={row.trained_on_live_verified_labels ? "ok" : "stop"}>
                    {row.trained_on_live_verified_labels ? "yes" : "no"}
                  </Tag>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Walk-forward folds</h3>
      <p className="lede">
        Time-ordered only. Every fold has a purge gap of at least the primary horizon between its
        training and test windows, because a label dated at the end of training is not settled until
        about S20 — without the gap, a training label&rsquo;s answer came from inside the test
        window.
      </p>
      {folds.length === 0 ? (
        <Empty what="walk-forward results" why="No model has been evaluated." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th className="num">Fold</th>
              <th>Train</th>
              <th>Test</th>
              <th className="num">Purge days</th>
              <th className="num">Train labels</th>
              <th className="num">Test labels</th>
              <th className="num">Score</th>
            </tr>
          </thead>
          <tbody>
            {folds.map((row) => (
              <tr key={`${row.run_id}-${row.fold_index}`}>
                <td>
                  <code>{row.model_version}</code>
                </td>
                <td className="num">{row.fold_index}</td>
                <td>
                  {row.train_start} → {row.train_end}
                </td>
                <td>
                  {row.test_start} → {row.test_end}
                </td>
                <td className="num">{row.purge_days}</td>
                <td className="num">{row.labels_in_train}</td>
                <td className="num">{row.labels_in_test}</td>
                <td className="num">
                  <Score value={row.score} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Calibration</h3>
      <p className="lede">
        Predicted probability against observed frequency. Until this table has rows,{" "}
        <strong>no probability is shown anywhere in this application</strong> — a model reporting
        0.73 without a calibration is reporting its opinion of itself.
      </p>
      {calibration.length === 0 ? (
        <Empty what="calibration bins" why="No model has been evaluated, so nothing is calibrated." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Bin</th>
              <th className="num">Predicted</th>
              <th className="num">Observed</th>
              <th className="num">Gap</th>
              <th className="num">Samples</th>
            </tr>
          </thead>
          <tbody>
            {calibration.map((row) => (
              <tr key={`${row.run_id}-${row.bin_lower}`}>
                <td>
                  <code>{row.model_version}</code>
                </td>
                <td>
                  {row.bin_lower}–{row.bin_upper}
                </td>
                <td className="num">
                  <Score value={row.predicted_mean} />
                </td>
                <td className="num">
                  <Score value={row.observed_rate} />
                </td>
                <td className="num">
                  <Score value={row.gap} />
                </td>
                <td className="num">{row.sample_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Promotion attempts</h3>
      <p className="lede">
        Refusals included, and they are the interesting rows. A log of successful promotions only
        cannot answer the question anybody actually asks, which is why the last one was refused.
      </p>
      {promotions.length === 0 ? (
        <Empty what="promotion attempts" why="Nothing has been put to the gate." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Outcome</th>
              <th>Challenger</th>
              <th>Champion</th>
              <th>Attempted</th>
              <th className="num">Mean score</th>
              <th>Why not</th>
            </tr>
          </thead>
          <tbody>
            {promotions.map((row) => (
              <tr key={row.attempt_id}>
                <td>
                  <Tag tone={row.promoted ? "ok" : "stop"}>
                    {row.promoted ? "promoted" : "refused"}
                  </Tag>
                </td>
                <td>
                  <code>{row.challenger_version}</code>
                </td>
                <td>{row.champion_version ? <code>{row.champion_version}</code> : "—"}</td>
                <td>
                  <When value={row.attempted_at} />
                </td>
                <td className="num">
                  <Score value={row.mean_score} />
                </td>
                <td>
                  {row.refusal_reasons.length === 0 ? (
                    "—"
                  ) : (
                    <ul>
                      {row.refusal_reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
