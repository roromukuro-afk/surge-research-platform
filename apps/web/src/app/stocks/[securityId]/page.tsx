import { notFound } from "next/navigation";
import { Empty, FailedToRead, Guard, Num, Stat, Tag } from "@/components/Chrome";
import { isConfigured, queryOne } from "@/lib/db";
import { DECISION_LABELS, STATE_LABELS, type Stage3State, type StockDetail } from "@/lib/contracts";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ securityId: string }>;
  searchParams: Promise<{ as_of?: string }>;
}

export default async function Stock({ params, searchParams }: Props) {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  const { securityId } = await params;
  const { as_of } = await searchParams;
  if (!as_of) notFound();

  let detail: StockDetail | null;
  try {
    const row = await queryOne<{ stock_detail: StockDetail }>(
      "select ui.stock_detail($1::uuid, $2::date) as stock_detail",
      [securityId, as_of],
    );
    detail = row?.stock_detail ?? null;
  } catch (error) {
    return <FailedToRead error={error} />;
  }

  if (!detail) return <Empty what="record for this security" />;

  const stage3 = detail.stage3 as
    | { state?: Stage3State; rationale?: string; reachable_zone_high?: string; reachable_zone_basis?: string }
    | null;
  const stage2 = detail.stage2 as Record<string, string | number | boolean | null> | null;

  return (
    <>
      <h2>
        <span className="mono">{securityId.slice(0, 8)}</span> · {detail.as_of_date}
      </h2>

      {detail.eligibility ? (
        <div className="cards">
          <Stat
            label="Price"
            value={<Num value={detail.eligibility.price} />}
            hint={detail.eligibility.price_currency ?? undefined}
          />
          <Stat label="In JPY" value={<Num value={detail.eligibility.converted_jpy} digits={0} />} />
          <Stat label="3,000 JPY rule" value={DECISION_LABELS[detail.eligibility.decision]} />
          <Stat
            label="Price age"
            value={<Num value={detail.eligibility.price_age_days} digits={0} />}
            hint="days"
          />
        </div>
      ) : null}

      <h2>How it was found</h2>
      <p className="lede">
        The two sides run independently and neither filters the other. A stock here on news alone is a
        candidate; so is one here on a chart signal alone.
      </p>
      <table>
        <tbody>
          <tr>
            <th>Technical routes</th>
            <td>
              {detail.technical_routes?.length
                ? detail.technical_routes.map((route) => <Tag key={route}>{route}</Tag>)
                : "—"}
            </td>
          </tr>
          <tr>
            <th>Material routes</th>
            <td>
              {detail.material_routes?.routes?.length
                ? detail.material_routes.routes.map((route) => <Tag key={route}>{route}</Tag>)
                : "—"}
            </td>
          </tr>
          <tr>
            <th>Closest relation</th>
            <td className="mono">{detail.material_routes?.strongest_relation ?? "—"}</td>
          </tr>
        </tbody>
      </table>

      {stage2 ? (
        <>
          <h2>Stage 2 measurements</h2>
          <p className="lede">
            The numbers are the record. Concept names are derived from them, not stored instead of them.
            Anything that could not be measured is listed as a gap rather than left as an ambiguous null.
          </p>
          <table>
            <tbody>
              {Object.entries(stage2)
                .filter(([key, value]) => value !== null && !key.endsWith("_id") && key !== "run_id")
                .slice(0, 40)
                .map(([key, value]) => (
                  <tr key={key}>
                    <th>{key.replace(/_/g, " ")}</th>
                    <td className="mono">{String(value)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      ) : null}

      <h2>What stands in the way</h2>
      <p className="lede">
        Resistance and overhang. <strong>These are not targets.</strong> A price this security once
        traded at is where sellers are waiting, not a reason it will return there — and an advance has to
        absorb them rather than aim at them.
      </p>
      {detail.price_obstacles.length ? (
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th className="num">Level</th>
              <th className="num">Distance</th>
              <th>Set on</th>
              <th className="num">Touches</th>
              <th>Has it weakened?</th>
            </tr>
          </thead>
          <tbody>
            {detail.price_obstacles.map((obstacle, index) => (
              <tr key={`${obstacle.kind}-${obstacle.price_level}-${index}`}>
                <td>
                  <Tag tone={obstacle.kind === "PRIOR_SURGE_HIGH" ? "stop" : undefined}>
                    {obstacle.kind.toLowerCase().replace(/_/g, " ")}
                  </Tag>
                </td>
                <td className="num">
                  <Num value={obstacle.price_level} />
                </td>
                <td className="num">
                  <Num value={obstacle.distance_pct} digits={1} />%
                </td>
                <td className="mono">{obstacle.established_on ?? "—"}</td>
                <td className="num mono">{obstacle.touch_count ?? "—"}</td>
                <td>
                  {obstacle.weakening_evidence ?? (
                    <span className="mono" style={{ opacity: 0.6 }}>
                      no evidence it has
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="obstacles above the price" />
      )}

      {stage3 ? (
        <>
          <h2>End-of-day analysis</h2>
          <div className="cards">
            <Stat
              label="State"
              value={stage3.state ? STATE_LABELS[stage3.state] : "—"}
              hint="Not an entry decision"
            />
            <Stat label="Reachable zone (upper)" value={<Num value={stage3.reachable_zone_high ?? null} />} />
          </div>
          {stage3.rationale ? <p className="lede">{stage3.rationale}</p> : null}
          {stage3.reachable_zone_basis ? (
            <p className="lede">
              <strong>Zone rests on:</strong> {stage3.reachable_zone_basis}
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}
