/**
 * The system as it is now (D-279): what Phase B is waiting for, what the cloud
 * is, and what has been checked. Nothing here is synthetic - the made-up
 * cohort the screens were built against is on /demo, and says so.
 */

import Link from "next/link";
import { Stat } from "@/components/Chrome";
import { OpsNotConfigured, PublishedAt, Yes, fmtJstAgo, openOps } from "@/components/OpsChrome";
import { cronVerifiedAt } from "@/lib/ops/status";

export const dynamic = "force-dynamic";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export default async function Operations() {
  const opened = await openOps();
  if ("problem" in opened) return <OpsNotConfigured problem={opened.problem} />;
  const { ops } = opened;
  const cloud = ops.cloud?.record ?? null;
  const pc = ops.pc?.record ?? null;
  const phase = pc?.phase_b ?? null;
  const checks = pc?.checks ?? null;
  const prediction = phase?.scheduler?.find((task) => !task.task.includes("outcome")) ?? null;
  const cron = cronVerifiedAt(ops.cloudHistory);
  const rehearsal = (checks?.rehearsal ?? {}) as Record<string, unknown>;
  const rehearsalSummary = (rehearsal.summary ?? null) as
    (Record<string, number> & { selected?: Record<string, number> }) | null;
  const drawn = rehearsalSummary?.selected ?? null;
  const comparison = (rehearsal.pc_comparison ?? null) as { compared: number; same: number } | null;

  return (
    <>
      <div className="notice">
        <strong>Phase B is prospective.</strong> The evaluation runs on the operator&apos;s PC from a frozen
        worktree; this deployment is its shadow - it reads the same universe with the same code and builds the
        same requests, and sends none. Until the first day (2026-09-24) these screens show the state of the
        system, not predictions. The synthetic cohort the screens were built against is on{" "}
        <Link href="/demo">/demo</Link>, and every screen there says it is made up.
      </div>

      <Section title="Phase B">
        <PublishedAt what="From the operator's machine," at={pc?.published_at} by={pc?.publisher} />
        <div className="cards">
          <Stat label="Status" value={readableStatus(phase?.status)}
                hint={phase ? `${phase.days.count} days written, ${phase.days.sent.length} sent` : undefined} />
          <Stat label="Prospective start" value={phase?.prospective_start ?? "—"}
                hint="the first S0 the cohort may draw from" />
          <Stat label="Next prediction run" value={readableTime(prediction?.next_run)}
                hint={prediction ? `${prediction.task} · ${prediction.state}` : "the PC's scheduled task"} />
          <Stat label="Model" value={phase?.model?.pinned_served ?? cloud?.phase_b.model ?? "—"}
                hint={phase ? `${phase.model.provider}, requested ${phase.model.requested}` : undefined} />
          <Stat label="TypeSafe credit"
                value={phase?.credit?.confirmed_balance_usd ? `$${phase.credit.confirmed_balance_usd}` : "—"}
                hint={phase?.credit?.displayed_expiry ? `expires ${phase.credit.displayed_expiry}` : undefined} />
          <Stat label="Global budget"
                value={phase?.budget?.global_hard_cap_usd ? `$${phase.budget.global_hard_cap_usd}` : "—"}
                hint={phase ? `spent $${phase.spend.spent_usd ?? "0.00"} · ${phase.per_day.max_requests} requests a day at most` : undefined} />
        </div>
      </Section>

      <Section title="The cloud">
        <PublishedAt what="Written by the deployment itself," at={cloud?.published_at}
                     by={cloud ? `a ${cloud.trigger} run` : undefined} />
        <div className="cards">
          <Stat label="Deployment"
                value={cloud?.deployment.commit ? cloud.deployment.commit.slice(0, 7) : "—"}
                hint={pc?.cloud.deployment.ready_state
                  ? `${pc.cloud.deployment.ready_state.toLowerCase()} · ${cloud?.deployment.region ?? "?"}`
                  : cloud?.deployment.id ?? undefined} />
          <Stat label="Input-building code"
                value={cloud ? cloud.code.input_building_code_sha256.slice(0, 12) + "…" : "—"}
                hint="the hash a day's manifest names; the PC's must match" />
          <Stat label="Frozen protocol"
                value={<Yes value={cloud?.code.matches_official_cohort ?? null} yes="matches the cohort" no="differs" />}
                hint={cloud?.code.frozen_protocol_fingerprint.slice(0, 12) + "…"} />
          <Stat label="Workflow readiness"
                value={<Yes value={checks?.before_the_deployment.all_ok ?? null} yes="checked" no="not checked" />}
                hint={checks?.before_the_deployment.at
                  ? `A-D on ${checks.before_the_deployment.at.slice(0, 10)}`
                  : "the C extensions, one, ten and 150 securities"} />
          <Stat label="Yahoo readiness"
                value={<Yes value={(checks?.before_the_deployment.checks?.D?.ok as boolean) ?? null}
                            yes="150 in one step" no="not checked" />}
                hint={readMeasure(checks?.before_the_deployment.checks?.D)} />
          <Stat label="Scheduled cron" value={cron ? "fires" : "not observed"}
                hint={cron ? fmtJstAgo(cron, ops.now) : "no cron-triggered record yet"} />
        </div>
      </Section>

      <Section title="The rehearsal">
        <p className="hint" style={{ marginTop: 0 }}>
          The whole pipeline on a past business day, in the cloud, under its own cohort
          (<code>surge/rehearsal</code>): the universe read from JPX, every security from Yahoo, the screener,
          the routes, Primary and Control drawn, the requests built and written to Blob. No request is sent,
          and nothing of it reaches the evaluation.
        </p>
        {comparison && comparison.same === comparison.compared ? (
          <p className="notice">
            Every count the PC&apos;s own rehearsal of the same day kept is the same here ({comparison.same} of{" "}
            {comparison.compared}): the JPX workbook, the universe, the coverage, the ¥3,000 population, the
            screener and every route. The PC&apos;s rehearsal drew no sample, so Primary and Control are not in
            that comparison; those are compared for the first time on 2026-09-24.
          </p>
        ) : null}
        {rehearsalSummary ? (
          <div className="cards">
            <Stat label="S0" value={String(rehearsal.s0 ?? "—")} hint={String(rehearsal.status ?? "")} />
            <Stat label="Universe" value={fmt(rehearsalSummary.issues)} hint="domestic common stock in JPX's list" />
            <Stat label="Read" value={fmt(rehearsalSummary.histories_read)}
                  hint={`coverage ${((rehearsalSummary.coverage ?? 0) * 100).toFixed(1)}%`} />
            <Stat label="At or under ¥3,000" value={fmt(rehearsalSummary.eligible)} hint="as traded on S0" />
            <Stat label="Passed the screener" value={fmt(rehearsalSummary.passing)} hint="Routes A-H" />
            <Stat label="Drawn" value={`${fmt(drawn?.primary)} primary`}
                  hint={drawn ? `${fmt(drawn.control)} control, ${fmt(drawn.anonymized)} anonymized, ${fmt(drawn.drift)} drift` : undefined} />
          </div>
        ) : (
          <div className="empty">
            <strong>The rehearsal has not been published yet.</strong>
            <p style={{ margin: "6px 0 0" }}>
              It is started in the cloud and its result is published from the operator&apos;s machine; until
              then <Link href="/runs">/runs</Link> shows the run itself.
            </p>
          </div>
        )}
      </Section>

      <p className="hint">
        More: <Link href="/system">the system</Link> in full, <Link href="/runs">every run</Link>, the{" "}
        <Link href="/compare">comparison</Link> of the PC and the cloud, and the{" "}
        <Link href="/cohort?cohort=rehearsal">rehearsal&apos;s own artifacts</Link>.
      </p>
    </>
  );
}

/** The publisher writes the state as the job knows it; a reader wants a phrase. */
function readableStatus(status: string | undefined): string {
  if (!status) return "unknown";
  return status === "waiting_for_start" ? "waiting for the first day" : status.replace(/_/g, " ");
}

/** Windows reports a task's next run in its own locale; show the date as everything else here is shown. */
function readableTime(value: string | undefined): string {
  if (!value) return "—";
  const us = value.match(/^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2})/);
  if (us) return `${us[3]}-${us[1]}-${us[2]} ${us[4]}:${us[5]} JST`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : value.slice(0, 16).replace("T", " ");
}

function fmt(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString("en-US");
}

function readMeasure(check: Record<string, unknown> | undefined): string | undefined {
  const measure = (check?.measure ?? null) as Record<string, number> | null;
  if (!measure) return "150 securities in one step, as a day reads them";
  return `${measure.read}/${measure.attempted} in ${measure.seconds}s, ${measure.failed} failed`;
}
