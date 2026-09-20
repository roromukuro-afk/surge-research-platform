/**
 * The PC's day against the cloud's, item by item (D-279).
 *
 * The comparison itself runs on the operator's machine - only it can read both
 * the evaluation's own artifacts and the cloud's - and publishes its report.
 * This screen shows that report and classifies nothing of its own: each item is
 * an exact match, a difference the timing explains, or an unexplained mismatch,
 * and the last of those is what decides whether a cloud Jev smoke may even be
 * proposed.
 */

import Link from "next/link";
import { Missing, OpsNotConfigured, PublishedAt, openOps } from "@/components/OpsChrome";
import { type CompareStatus, type ItemStatus, newestStatus } from "@/lib/ops/status";

export const dynamic = "force-dynamic";

const STATUS_LABEL: Record<ItemStatus, { label: string; tone: string }> = {
  exact_match: { label: "exact match", tone: "ok" },
  expected_timing_difference: { label: "expected timing difference", tone: "warn" },
  unexplained_mismatch: { label: "unexplained mismatch", tone: "stop" },
};

/** What each item of the report is, in the order a reader wants them. */
const TITLES: [string, string][] = [
  ["universe", "The securities JPX lists, and the workbook they came from"],
  ["population_at_or_below_3000_yen", "Those at or under ¥3,000 as traded on S0"],
  ["screening_pass", "What the screener passed"],
  ["route_memberships", "Which routes each passing security belongs to"],
  ["primary", "The Primary sample"],
  ["control", "The Control sample and how each was matched"],
  ["selected_history_digests", "The as-traded history of every selected security"],
  ["disclosure_titles", "The TDnet titles each request carries"],
  ["request_hashes_same_input", "The request bodies built from the same inputs"],
  ["request_hashes_independent_input", "The request bodies each side built from what it read"],
  ["sessions", "The sessions the data shows"],
  ["manifest", "The manifest, everything but when it started"],
];

function summarise(item: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of ["same", "only_pc", "only_cloud", "differing", "requests"]) {
    const value = item[key];
    if (typeof value === "number") parts.push(`${key.replace(/_/g, " ")}: ${value}`);
    else if (Array.isArray(value) && value.length) parts.push(`${key.replace(/_/g, " ")}: ${value.length}`);
  }
  const byStatus = item.by_status as Record<string, number> | undefined;
  if (byStatus) {
    parts.push(
      Object.entries(byStatus)
        .filter(([, n]) => n)
        .map(([status, n]) => `${n} ${STATUS_LABEL[status as ItemStatus]?.label ?? status}`)
        .join(", "),
    );
  }
  if (item.workbook_sha256) {
    const workbook = item.workbook_sha256 as { same?: boolean };
    parts.push(workbook.same ? "the same JPX workbook" : "a different JPX workbook");
  }
  return parts.filter(Boolean).join(" · ");
}

export default async function Compare() {
  const opened = await openOps();
  if ("problem" in opened) return <OpsNotConfigured problem={opened.problem} />;
  const { ops } = opened;
  const published = await newestStatus<CompareStatus>(ops.source, "compare", ops.now);
  const record = published?.record ?? null;

  if (!record) {
    return (
      <>
        <section className="panel">
          <h2>The PC and the cloud, compared</h2>
          <p className="hint" style={{ marginTop: 0 }}>
            The first comparison is of S0 2026-09-24, the cohort&apos;s first prospective day: the PC runs the
            day at 16:10 JST, the cloud runs its shadow of it afterwards, and the two are compared item by
            item. Until then there is nothing to compare - the cohort may not draw from an earlier day, so the
            cloud&apos;s <Link href="/cohort?cohort=rehearsal">rehearsal</Link> is of its own cohort, not of a
            day the PC has run. This is what will be on this screen:
          </p>
          <table>
            <thead>
              <tr><th>Item</th><th>What is compared</th></tr>
            </thead>
            <tbody>
              {TITLES.map(([id, what]) => (
                <tr key={id}>
                  <td className="mono">{id}</td>
                  <td>{what}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <Missing what="a comparison" />
      </>
    );
  }

  const entries = TITLES.filter(([id]) => record.items[id]).map(([id, title]) => ({
    id,
    title,
    item: record.items[id],
  }));
  const counts = entries.reduce<Record<string, number>>((acc, entry) => {
    acc[entry.item.status] = (acc[entry.item.status] ?? 0) + 1;
    return acc;
  }, {});
  const unexplained = counts.unexplained_mismatch ?? 0;

  return (
    <>
      <section className="panel">
        <h2>S0 {record.s0}: the PC&apos;s day against the cloud&apos;s</h2>
        <PublishedAt what="Compared on the operator's machine," at={record.published_at} />
        <div className="cards">
          {(Object.keys(STATUS_LABEL) as ItemStatus[]).map((status) => (
            <div className="card" key={status}>
              <div className="label">{STATUS_LABEL[status].label}</div>
              <div className="value">{counts[status] ?? 0}</div>
              <div className="hint">
                {status === "unexplained_mismatch" ? "must be zero before a Jev smoke is proposed" : ""}
              </div>
            </div>
          ))}
          <div className="card">
            <div className="label">The two days</div>
            <div className="value">
              {record.pc_status} / {record.cloud_status}
            </div>
            <div className="hint">the PC&apos;s status, then the cloud&apos;s</div>
          </div>
        </div>
        <p className={unexplained ? "notice stop" : "notice"}>
          {unexplained
            ? `${unexplained} item${unexplained === 1 ? "" : "s"} the timing does not explain. Nothing is proposed while any remains: ` +
              Object.entries(record.stage4.unexplained_by_group)
                .filter(([, names]) => names.length)
                .map(([group, names]) => `${group} (${names.join(", ")})`)
                .join("; ")
            : record.stage4.may_be_proposed
              ? "Every item is accounted for. A single cloud Jev smoke may be proposed to the operator; it is never started here."
              : "No unexplained mismatch is shown, but the report does not allow a proposal."}
        </p>
        <p className="hint">{record.stage4.rule}</p>
      </section>

      <section className="panel">
        <h2>Item by item</h2>
        <table>
          <thead>
            <tr><th>Item</th><th>Status</th><th>What it says</th></tr>
          </thead>
          <tbody>
            {entries.map(({ id, title, item }) => (
              <tr key={id}>
                <td>
                  <span className="mono">{id}</span>
                  <div className="hint">{title}</div>
                </td>
                <td>
                  <span className={`tag ${STATUS_LABEL[item.status]?.tone ?? ""}`}>
                    {STATUS_LABEL[item.status]?.label ?? item.status}
                  </span>
                </td>
                <td className="hint">{summarise(item) || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
