import Link from "next/link";
import { Empty, FailedToRead, Guard, Num, Stat, Tag } from "@/components/Chrome";
import { isConfigured, query } from "@/lib/db";
import type { DashboardRow, NotLiveVerifiedRow, UnfilledRole } from "@/lib/contracts";

export const dynamic = "force-dynamic";

async function load() {
  const [days, unfilled, notVerified] = await Promise.all([
    query<DashboardRow>("select * from ui.dashboard_daily order by as_of_date desc, market_code limit 14"),
    query<UnfilledRole>("select * from ui.unfilled_roles order by domain, role"),
    query<NotLiveVerifiedRow>("select * from ui.not_live_verified order by component, name"),
  ]);
  return { days, unfilled, notVerified };
}

export default async function Dashboard() {
  if (!isConfigured()) return <Guard>{null}</Guard>;

  let data;
  try {
    data = await load();
  } catch (error) {
    return <FailedToRead error={error} />;
  }
  const { days, unfilled, notVerified } = data;
  const latest = days[0];
  const standIn = latest ? latest.from_the_stand_in : 0;

  return (
    <>
      <h2>Today</h2>
      <p className="lede">
        What the end-of-day run produced. The technical and material sides nominate independently and
        neither filters the other, so a stock can appear here on a chart signal alone, on news alone, or
        on both.
      </p>

      {standIn > 0 ? (
        <div className="notice">
          <strong>{standIn} of these verdicts came from the deterministic stand-in.</strong> No model was
          called. The stand-in exercises the pipeline and says nothing about any security; treat these
          states as evidence the plumbing runs, and as nothing else.
        </div>
      ) : null}

      {latest ? (
        <div className="cards">
          <Stat label="Price eligible" value={latest.price_eligible} hint="3,000 JPY rule, in JPY terms" />
          <Stat
            label="Technical candidates"
            value={latest.technical_candidates}
            hint="Routes A–H, OR-type"
          />
          <Stat
            label="Material candidates"
            value={latest.material_candidates}
            hint="Routes M1–M6, run independently"
          />
          <Stat label="Setups" value={latest.technical_setups + latest.catalyst_setups} />
          <Stat label="Watching" value={latest.watching} />
          <Stat
            label="Failed validation"
            value={latest.failed_validation}
            hint="Answers the validator refused"
          />
        </div>
      ) : (
        <Empty
          what="runs"
          why="No end-of-day run has been recorded. The pipeline is complete and no price provider is contracted yet — see the two decisions on the pipeline screen."
        />
      )}

      {days.length > 1 ? (
        <>
          <h2>Recent days</h2>
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Market</th>
                <th className="num">Eligible</th>
                <th className="num">Stale</th>
                <th className="num">Technical</th>
                <th className="num">Material</th>
                <th className="num">Setups</th>
                <th className="num">Watching</th>
                <th className="num">Rejected</th>
              </tr>
            </thead>
            <tbody>
              {days.map((day) => (
                <tr key={`${day.as_of_date}-${day.market_code}`}>
                  <td className="mono">{day.as_of_date}</td>
                  <td>{day.market_code}</td>
                  <td className="num">
                    <Num value={day.price_eligible} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.stale_inputs} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.technical_candidates} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.material_candidates} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.technical_setups + day.catalyst_setups} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.watching} digits={0} />
                  </td>
                  <td className="num">
                    <Num value={day.rejected} digits={0} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}

      <h2>Roles nothing fills</h2>
      <p className="lede">
        Kept on the dashboard rather than in a report. An unfilled role is invisible until someone asks
        why a number is zero.
      </p>
      {unfilled.length ? (
        <table>
          <thead>
            <tr>
              <th>Role</th>
              <th>Domain</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {unfilled.map((role) => (
              <tr key={`${role.domain}-${role.role}`}>
                <td className="mono">{role.role}</td>
                <td>{role.domain}</td>
                <td>{role.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty what="unfilled roles" why="Every role has a provider bound to it." />
      )}

      <h2>Built, never met real data</h2>
      <p className="lede">
        The <span className="mono">IMPLEMENTED_NOT_LIVE_VERIFIED</span> list. Everything here has code and
        tests and has never spoken to the real thing, so &ldquo;it is built&rdquo; cannot quietly become
        &ldquo;it works&rdquo;.
      </p>
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

      <p className="lede" style={{ marginTop: 26 }}>
        <Link href="/diagnostics">Pipeline diagnostics</Link> has the runs behind these numbers.
      </p>
    </>
  );
}
