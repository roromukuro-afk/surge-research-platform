import { Empty, FailedToRead, Guard, Tag } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type {
  ConceptPerformanceRow,
  LlmAuditRow,
  MaterialDriverRow,
  MissBreakdownRow,
  RoutePerformanceRow,
  TeacherSummaryRow,
} from "@/lib/contracts";

export const dynamic = "force-dynamic";

const WHOSE_TONE: Record<string, "ok" | "warn" | "stop" | undefined> = {
  "prediction model": "stop",
  "collection pipeline": "warn",
  nobody: undefined,
  timing: "warn",
};

function Pct({ value }: { value: string | null }) {
  if (value === null) return <>—</>;
  return <>{(Number(value) * 100).toFixed(1)}%</>;
}

/** Counts, never a rate. A hit rate over four episodes is not a hit rate. */
function Counts({ hit, failed, unresolved }: { hit: string; failed: string; unresolved: string }) {
  return (
    <>
      {hit} / {failed} / {unresolved}
    </>
  );
}

export default async function Improvement() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let teacher: TeacherSummaryRow[];
  let misses: MissBreakdownRow[];
  let routes: RoutePerformanceRow[];
  let drivers: MaterialDriverRow[];
  let concepts: ConceptPerformanceRow[];
  let judgements: LlmAuditRow[];
  try {
    [teacher, misses, routes, drivers, concepts, judgements] = await Promise.all([
      query<TeacherSummaryRow>(`select * from ui.teacher_data_summary order by observation_kind`),
      query<MissBreakdownRow>(`select * from ui.miss_breakdown order by miss_kind`),
      query<RoutePerformanceRow>(
        `select * from ui.route_performance order by route_kind, route limit 100`,
      ),
      query<MaterialDriverRow>(
        `select * from ui.material_driver_performance order by episodes desc limit 100`,
      ),
      query<ConceptPerformanceRow>(
        `select * from ui.concept_performance order by episodes desc limit 100`,
      ),
      query<LlmAuditRow>(
        `select * from ui.llm_judgement_audit order by as_of_date desc limit 100`,
      ),
    ]);
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const totalObservations = teacher.reduce((sum, row) => sum + Number(row.observations), 0);

  return (
    <>
      <h2>Continuous improvement</h2>
      <p className="lede">
        What the teacher set contains, which routes and materials the outcomes came from, and
        whether a miss was the model&rsquo;s, the pipeline&rsquo;s, or nobody&rsquo;s. Counts are
        shown rather than rates: a hit rate over four episodes is not a hit rate.
      </p>

      {totalObservations === 0 ? (
        <div className="notice">
          <strong>There is no teacher data yet, which is the correct state.</strong> The first
          production teacher row comes from a real episode whose outcome was decided — not from a
          fixture, a synthetic run, or a historical replay. Everything below reads zero because
          zero is the honest number.
        </div>
      ) : null}

      <h3>Teacher population</h3>
      <p className="lede">
        All three kinds are in it. A set consisting only of <code>PREDICTED</code> would teach a
        model what this system already believed and make every miss invisible to it.
      </p>
      {teacher.length === 0 ? (
        <Empty what="observations" why="No episode has produced an outcome." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>How it arose</th>
              <th className="num">Observations</th>
              <th className="num">Reached +20%</th>
              <th className="num">Labelled</th>
              <th className="num">Approved</th>
            </tr>
          </thead>
          <tbody>
            {teacher.map((row) => (
              <tr key={row.observation_kind}>
                <td>{row.observation_kind}</td>
                <td className="num">{row.observations}</td>
                <td className="num">{row.reached_target}</td>
                <td className="num">{row.labelled}</td>
                <td className="num">{row.approved}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Misses, by whose failure they were</h3>
      <p className="lede">
        Three different findings, kept apart. Presenting one &ldquo;missed&rdquo; number would let a
        collector outage read as a model failure and a model failure read as bad luck.
      </p>
      {misses.length === 0 ? (
        <Empty what="misses" why="Nothing has been adjudicated." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Whose failure</th>
              <th className="num">Episodes</th>
              <th className="num">Approved</th>
            </tr>
          </thead>
          <tbody>
            {misses.map((row) => (
              <tr key={row.miss_kind}>
                <td>{row.miss_kind}</td>
                <td>
                  <Tag tone={WHOSE_TONE[row.whose_failure ?? ""]}>{row.whose_failure}</Tag>
                </td>
                <td className="num">{row.episodes}</td>
                <td className="num">{row.approved}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>By route</h3>
      <p className="lede">
        Target / failure / unresolved. The unresolved episodes are counted rather than dropped: a
        hit rate that quietly omits the ambiguous ones is a hit rate over the cases that happened to
        be easy to measure.
      </p>
      {routes.length === 0 ? (
        <Empty what="route results" why="No episode has closed." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Route</th>
              <th>Kind</th>
              <th className="num">Episodes</th>
              <th className="num">Target / failure / unresolved</th>
              <th className="num">Mean MFE</th>
              <th className="num">Mean MAE</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((row) => (
              <tr key={`${row.route_kind}-${row.route}`}>
                <td>{row.route}</td>
                <td>{row.route_kind}</td>
                <td className="num">{row.episodes}</td>
                <td className="num">
                  <Counts
                    hit={row.reached_target}
                    failed={row.hit_failure}
                    unresolved={row.unresolved}
                  />
                </td>
                <td className="num">
                  <Pct value={row.mean_mfe} />
                </td>
                <td className="num">
                  <Pct value={row.mean_mae} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>By material driver</h3>
      {drivers.length === 0 ? (
        <Empty what="material drivers" why="No episode has closed." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Disclosure type</th>
              <th className="num">Episodes</th>
              <th className="num">Target / failure / unresolved</th>
            </tr>
          </thead>
          <tbody>
            {drivers.map((row) => (
              <tr key={row.driver}>
                <td>{row.driver}</td>
                <td className="num">{row.episodes}</td>
                <td className="num">
                  <Counts
                    hit={row.reached_target}
                    failed={row.hit_failure}
                    unresolved={row.unresolved}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>By chart concept</h3>
      {concepts.length === 0 ? (
        <Empty what="concept results" why="No episode has closed." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Concept</th>
              <th className="num">Episodes</th>
              <th className="num">Reached target</th>
              <th className="num">Hit failure</th>
              <th className="num">Mean MFE</th>
            </tr>
          </thead>
          <tbody>
            {concepts.map((row) => (
              <tr key={row.concept}>
                <td>{row.concept}</td>
                <td className="num">{row.episodes}</td>
                <td className="num">{row.reached_target}</td>
                <td className="num">{row.hit_failure}</td>
                <td className="num">
                  <Pct value={row.mean_mfe} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Model judgements</h3>
      <p className="lede">
        What the model said and how often the validator rejected it. The stand-in column is on every
        row because an unlabelled mock verdict in a results table is indistinguishable from a real
        one a year later.
      </p>
      {judgements.length === 0 ? (
        <Empty what="judgements" why="Stage 3 has not run against a populated bundle." />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Provider</th>
              <th>State</th>
              <th>Validation</th>
              <th className="num">Answers</th>
              <th className="num">From a stand-in</th>
            </tr>
          </thead>
          <tbody>
            {judgements.map((row, index) => (
              <tr key={`${row.as_of_date}-${row.provider_id}-${row.state}-${index}`}>
                <td>{row.as_of_date}</td>
                <td>
                  <code>{row.provider_id}</code>
                  {row.provider_kind === "DETERMINISTIC_MOCK" ? (
                    <Tag tone="warn">stand-in</Tag>
                  ) : null}
                </td>
                <td>{row.state}</td>
                <td>{row.validation_status}</td>
                <td className="num">{row.answers}</td>
                <td className="num">{row.from_a_stand_in}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
