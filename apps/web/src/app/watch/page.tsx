import Link from "next/link";
import { Empty, FailedToRead, Guard, Num, Tag } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import { STATE_LABELS, type Stage3State, type WatchRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const TONE: Record<Stage3State, "ok" | "warn" | "stop" | undefined> = {
  TECHNICAL_SETUP_EOD: "ok",
  POST_CLOSE_CATALYST_SETUP: "ok",
  WATCH_BREAKOUT: "warn",
  WATCH_PULLBACK: "warn",
  WATCH_OTHER: "warn",
  REJECT: "stop",
};

export default async function Watch() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: WatchRow[];
  try {
    rows = await query<WatchRow>(
      `select * from ui.watch_and_setup
       where as_of_date = (select max(as_of_date) from ui.watch_and_setup)
         and state <> 'REJECT'
       order by state, security_id
       limit 200`,
    );
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const fromStandIn = rows.filter((row) => row.provider_kind === "DETERMINISTIC_MOCK").length;

  return (
    <>
      <h2>Setups and watches</h2>
      <p className="lede">
        End-of-day analysis. <strong>None of these is an entry.</strong> An entry is a decision taken
        during a session against a live price; the states available here stop at setup, watch and reject,
        and the output type has no entry member at all.
      </p>

      {fromStandIn > 0 ? (
        <div className="notice">
          <strong>{fromStandIn} of these came from the deterministic stand-in.</strong> It applies fixed
          rules to the bundle and returns the same answer every time. That demonstrates the pipeline
          runs; it demonstrates nothing about any security.
        </div>
      ) : null}

      {rows.length === 0 ? (
        <Empty
          what="setups or watches"
          why="Stage 3 has not run against a populated bundle. The bundle, the prompt assembly, the validator and the storage are all in place."
        />
      ) : (
        <table>
          <thead>
            <tr>
              <th>State</th>
              <th>Security</th>
              <th>Why</th>
              <th className="num">+20% level</th>
              <th className="num">Reachable zone</th>
              <th>Zone rests on</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.security_id}-${row.as_of_date}`}>
                <td>
                  <Tag tone={TONE[row.state]}>{STATE_LABELS[row.state]}</Tag>
                </td>
                <td className="mono">
                  <Link href={`/stocks/${row.security_id}?as_of=${row.as_of_date}`}>
                    {row.security_id.slice(0, 8)}
                  </Link>
                </td>
                <td style={{ maxWidth: "34ch" }}>{row.rationale}</td>
                <td className="num">
                  <Num value={row.twenty_percent_threshold_price} />
                </td>
                <td className="num">
                  {row.reachable_zone_high ? (
                    <>
                      <Num value={row.reachable_zone_low} /> – <Num value={row.reachable_zone_high} />
                    </>
                  ) : (
                    <span className="mono">—</span>
                  )}
                </td>
                <td>
                  {row.reachable_zone_basis_kinds?.length ? (
                    row.reachable_zone_basis_kinds.map((kind) => (
                      <Tag key={kind}>{kind.toLowerCase().replace(/_/g, " ")}</Tag>
                    ))
                  ) : (
                    <span className="mono">—</span>
                  )}
                </td>
                <td>
                  <Tag tone={row.provider_kind === "DETERMINISTIC_MOCK" ? "warn" : undefined}>
                    {row.provider_id}
                  </Tag>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="notice" style={{ marginTop: 22 }}>
        <strong>The +20% level and the reachable zone are different things.</strong> The first is
        arithmetic on a reference price. The second is a judgement about how far the price could
        plausibly travel, and it must rest on current material, supply, volume, support and resistance or
        volatility — never on the fact that the stock once traded higher.
      </div>
    </>
  );
}
