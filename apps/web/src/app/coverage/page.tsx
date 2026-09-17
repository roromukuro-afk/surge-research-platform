import { Empty, FailedToRead, Guard, Tag } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type { CoverageRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const TONE: Record<string, "ok" | "warn" | "stop"> = {
  COMPLETE: "ok",
  PARTIAL_KNOWN_GAP: "warn",
  NOT_PROVIDED: "warn",
  UNKNOWN: "stop",
};

export default async function Coverage() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: CoverageRow[];
  try {
    rows = await query<CoverageRow>(
      "select * from ui.coverage_summary order by domain, as_of_date desc nulls last, component limit 300",
    );
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  return (
    <>
      <h2>Coverage</h2>
      <p className="lede">
        Three kinds of trouble, kept in three columns. A provider error is an outage at their end. A
        parse error is a bug at ours. A quality warning is the provider telling us the data is thin. They
        call for different fixes, and adding them together would hide which one is happening.
      </p>

      {rows.length === 0 ? (
        <Empty
          what="coverage records"
          why="Nothing has been ingested. The counters exist and separate the three cases; there is nothing to count yet."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>Domain</th>
              <th>Date</th>
              <th>Component</th>
              <th className="num">Records</th>
              <th className="num">Provider errors</th>
              <th className="num">Our parse errors</th>
              <th className="num">Quality warnings</th>
              <th>Quality</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={`${row.domain}-${row.component}-${row.as_of_date ?? index}`}>
                <td>{row.domain}</td>
                <td className="mono">{row.as_of_date ?? "—"}</td>
                <td className="mono">{row.component}</td>
                <td className="num mono">{row.records}</td>
                <td className="num mono">{row.provider_errors}</td>
                <td className="num mono">{row.our_parse_errors}</td>
                <td className="num mono">{row.quality_warnings}</td>
                <td>
                  <Tag tone={TONE[row.quality]}>{row.quality}</Tag>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="notice" style={{ marginTop: 22 }}>
        <strong>An empty day and a broken collector look identical from a row count.</strong> A news
        source that publishes irregularly is legitimately quiet at weekends, so a listing of zero is
        recorded as <span className="mono">UNKNOWN</span> quality rather than as either success or
        failure. Deciding which it was needs the source&rsquo;s own schedule.
      </div>
    </>
  );
}
