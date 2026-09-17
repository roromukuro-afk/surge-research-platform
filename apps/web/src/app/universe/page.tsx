import Link from "next/link";
import { Empty, FailedToRead, Guard, Num, Stat, Tag } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import { DECISION_LABELS, type PriceDecision, type UniverseRow } from "@/lib/contracts";

export const dynamic = "force-dynamic";

const TONE: Partial<Record<PriceDecision, "ok" | "warn" | "stop">> = {
  PRICE_ELIGIBLE: "ok",
  STALE_PRICE: "warn",
  STALE_FX: "warn",
  PRICE_MISSING: "stop",
  FX_MISSING: "stop",
};

export default async function Universe() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let rows: UniverseRow[];
  try {
    rows = await query<UniverseRow>(
      `select * from ui.universe_eligibility
       where as_of_date = (select max(as_of_date) from ui.universe_eligibility)
       order by price_decision, native_symbol
       limit 500`,
    );
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    acc[row.price_decision] = (acc[row.price_decision] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <>
      <h2>Universe and the 3,000 JPY rule</h2>
      <p className="lede">
        Every security the rule was applied to, and the outcome — including the outcomes where it could
        not be applied. A price too old to trust and a price above the threshold are different answers,
        and neither is hidden inside &ldquo;excluded&rdquo;.
      </p>

      {rows.length === 0 ? (
        <Empty
          what="eligibility decisions"
          why="No price data has been ingested. The rule, its versioned thresholds and the staleness bounds are in place; the provider is not."
        />
      ) : (
        <>
          <div className="cards">
            {(Object.keys(DECISION_LABELS) as PriceDecision[])
              .filter((decision) => counts[decision])
              .map((decision) => (
                <Stat key={decision} label={DECISION_LABELS[decision]} value={counts[decision] ?? 0} />
              ))}
          </div>

          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Outcome</th>
                <th className="num">Price</th>
                <th>Currency</th>
                <th className="num">In JPY</th>
                <th className="num">FX age (s)</th>
                <th className="num">Price age (d)</th>
                <th>Rule</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.security_id}-${row.as_of_date}`}>
                  <td className="mono">
                    <Link href={`/stocks/${row.security_id}?as_of=${row.as_of_date}`}>
                      {row.native_symbol}
                    </Link>
                  </td>
                  <td>
                    <Tag tone={TONE[row.price_decision]}>{DECISION_LABELS[row.price_decision]}</Tag>
                  </td>
                  <td className="num">
                    <Num value={row.price} />
                  </td>
                  <td>{row.price_currency ?? "—"}</td>
                  <td className="num">
                    <Num value={row.price_jpy} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={row.fx_age_seconds} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={row.price_age_days} digits={0} />
                  </td>
                  <td className="mono">{row.rule_version}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
