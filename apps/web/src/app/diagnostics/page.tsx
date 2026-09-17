import { Empty, FailedToRead, Guard, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type { NotLiveVerifiedRow, RunRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const STATUS_TONE: Record<string, "ok" | "warn" | "stop"> = {
  SUCCEEDED: "ok",
  RUNNING: "warn",
  SKIPPED: "warn",
  FAILED: "stop",
};

export default async function Diagnostics() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let runs: RunRow[];
  let notVerified: NotLiveVerifiedRow[];
  try {
    [runs, notVerified] = await Promise.all([
      query<RunRow>("select * from ui.pipeline_runs order by started_at desc limit 60"),
      query<NotLiveVerifiedRow>("select * from ui.not_live_verified order by component, name"),
    ]);
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  return (
    <>
      <h2>Runs</h2>
      <p className="lede">
        A run that finished is not a run whose results count. Publication is what makes a run
        authoritative, and downstream reads go through the publication rather than through the newest
        finish time — so the two are shown as separate columns here.
      </p>

      {runs.length === 0 ? (
        <Empty what="runs" />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Version</th>
              <th>Mode</th>
              <th>Market</th>
              <th>As of</th>
              <th>Status</th>
              <th>Published</th>
              <th>Started</th>
              <th>Commit</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td className="mono">{run.job_name}</td>
                <td className="mono">{run.job_version}</td>
                <td>{run.run_mode}</td>
                <td>{run.market_code ?? "—"}</td>
                <td className="mono">{run.as_of_date ?? "—"}</td>
                <td>
                  <Tag tone={STATUS_TONE[run.status]}>{run.status}</Tag>
                </td>
                <td>
                  {run.published ? (
                    <Tag tone="ok">published</Tag>
                  ) : (
                    <Tag>not authoritative</Tag>
                  )}
                </td>
                <td>
                  <When value={run.started_at} />
                </td>
                <td className="mono">{run.git_sha ? run.git_sha.slice(0, 8) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Waiting on a decision only you can make</h2>
      <p className="lede">
        Four questions block live data, and none of them can be answered from code. They are recorded in{" "}
        <span className="mono">docs/unresolved-decisions.md</span>.
      </p>
      <table>
        <thead>
          <tr>
            <th>Ref</th>
            <th>Blocks</th>
            <th>What is needed</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className="mono">D-102</td>
            <td>Japanese end-of-day prices</td>
            <td>
              A written enquiry to the exchange about whether keeping published statistics in a private
              database counts as secondary use. The terms prohibit it without defining it.
            </td>
          </tr>
          <tr>
            <td className="mono">D-103</td>
            <td>US end-of-day prices</td>
            <td>
              One authenticated call settles whether the free plan serves full consolidated data; the
              residency clause needs a written answer from the broker.
            </td>
          </tr>
          <tr>
            <td className="mono">D-104</td>
            <td>Where the daily job runs</td>
            <td>
              Confirmation that the daily collection will not run on hosted CI runners, whose terms limit
              them to the repository&rsquo;s own software lifecycle.
            </td>
          </tr>
          <tr>
            <td className="mono">D-105</td>
            <td>Object storage</td>
            <td>
              A card on file before a bucket can be created. Overage is billed rather than stopped, so
              staying inside the free tier is a policy we keep, not one the provider enforces.
            </td>
          </tr>
        </tbody>
      </table>

      <h2>Implemented, never live-verified</h2>
      {notVerified.length ? (
        <table>
          <thead>
            <tr>
              <th>Component</th>
              <th>Name</th>
              <th>What that means</th>
            </tr>
          </thead>
          <tbody>
            {notVerified.map((row) => (
              <tr key={`${row.component}-${row.name}`}>
                <td>
                  <Tag tone="warn">{row.component}</Tag>
                </td>
                <td className="mono">{row.name}</td>
                <td>{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="unverified components" />
      )}
    </>
  );
}
