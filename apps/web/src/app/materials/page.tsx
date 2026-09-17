import { Empty, FailedToRead, Guard, Tag, When } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type { MaterialRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const RELEVANCE_TONE: Record<string, "ok" | "warn" | "stop"> = {
  RELEVANT: "ok",
  UNCERTAIN: "warn",
  NOT_RELEVANT: "stop",
};

export default async function Materials() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: MaterialRow[];
  try {
    // Read through the as-of function, never off the tables. The cutoff is
    // clock_timestamp() rather than now(): now() is transaction start, which
    // would hide anything published while the page was loading.
    rows = await query<MaterialRow>("select * from ui.materials_as_of(clock_timestamp()) limit 200");
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  return (
    <>
      <h2>Materials</h2>
      <p className="lede">
        One row per event, however many articles reported it. <strong>Independent sources</strong> counts
        distinct publishers that discovered or verified the event; reprints are counted separately as
        corroboration, so twenty copies of one wire story read as one source rather than twenty
        confirmations.
      </p>

      {rows.length === 0 ? (
        <Empty
          what="events"
          why="No collector has run against a live source. The schema, the four knowledge timestamps, the merge rule and the relevance ruleset are in place; the sources are not enabled."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>First knowable</th>
              <th>Type</th>
              <th>Headline</th>
              <th className="num">Independent</th>
              <th className="num">Reprints</th>
              <th className="num">Securities</th>
              <th>Relevance</th>
              <th>Closest relation</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.event_id}>
                <td>
                  <When value={row.first_known_at} />
                </td>
                <td className="mono">{row.event_type}</td>
                <td>{row.headline ?? <span className="mono">—</span>}</td>
                <td className="num mono">{row.independent_sources}</td>
                <td className="num mono">{row.corroborations}</td>
                <td className="num mono">{row.securities}</td>
                <td>
                  {row.relevance ? (
                    <Tag tone={RELEVANCE_TONE[row.relevance]}>{row.relevance}</Tag>
                  ) : (
                    <span className="mono">—</span>
                  )}
                  {row.relevance_reason ? (
                    <div className="mono" style={{ fontSize: 11.5, opacity: 0.75 }}>
                      {row.relevance_reason}
                    </div>
                  ) : null}
                </td>
                <td className="mono">{row.strongest_relation ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="notice" style={{ marginTop: 22 }}>
        <strong>UNCERTAIN is not a synonym for excluded.</strong> A macro claim whose mechanism the
        extractor could not name is held here for review rather than discarded, because the mechanism
        usually exists and the extractor usually missed it.
      </div>
    </>
  );
}
