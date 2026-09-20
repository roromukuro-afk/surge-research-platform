/**
 * The system in full (D-279): the deployment and the code it carries, the
 * checks that were run against it, the free quota, and the official Phase B on
 * the operator's machine. Every value says where it came from and when; none
 * is a secret, and nothing is inferred.
 */

import Link from "next/link";
import { Missing, OpsNotConfigured, PublishedAt, Row, Yes, fmtJstAgo, openOps } from "@/components/OpsChrome";
import { cronVerifiedAt } from "@/lib/ops/status";

export const dynamic = "force-dynamic";

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function Quota({ used, limit, over }: { used: string; limit: string; over?: boolean }) {
  return (
    <span className="mono">
      {used} / {limit} {over ? <span className="tag stop">over</span> : null}
    </span>
  );
}

export default async function System() {
  const opened = await openOps();
  if ("problem" in opened) return <OpsNotConfigured problem={opened.problem} />;
  const { ops } = opened;
  const cloud = ops.cloud?.record ?? null;
  const pc = ops.pc?.record ?? null;
  const cron = cronVerifiedAt(ops.cloudHistory);
  const checks = pc?.checks.before_the_deployment.checks ?? {};
  const usage = (pc?.usage ?? {}) as Record<string, Record<string, unknown> & { used_gb?: number; limit_gb?: number }>;
  const rehearsal = (pc?.checks.rehearsal ?? {}) as Record<string, unknown>;

  return (
    <>
      <Panel title="This deployment">
        <PublishedAt what="Written by the deployment itself on a no-op run," at={cloud?.published_at}
                     by={cloud ? cloud.trigger : undefined} />
        {cloud ? (
          <table>
            <tbody>
              <Row label="Commit">
                <span className="mono">{cloud.deployment.commit ?? "unknown"}</span>
              </Row>
              <Row label="Deployment">
                <span className="mono">{cloud.deployment.id ?? "—"}</span>{" "}
                {pc?.cloud.deployment.ready_state ? (
                  <span className="tag ok">{pc.cloud.deployment.ready_state.toLowerCase()}</span>
                ) : null}
              </Row>
              <Row label="Target and region">
                <span className="mono">{cloud.deployment.target ?? "—"} · {cloud.deployment.region ?? "—"}</span>
              </Row>
              <Row label="Runtime">
                <span className="mono">Python {cloud.deployment.python}</span>{" "}
                <span className="hint">
                  {Object.entries(cloud.deployment.packages)
                    .map(([name, version]) => `${name} ${version ?? "—"}`)
                    .join(" · ")}
                </span>
              </Row>
              <Row label="Input-building code" hint="the SHA-256 a day's manifest names">
                <span className="mono">{cloud.code.input_building_code_sha256}</span>
              </Row>
              <Row label="Frozen protocol" hint="a day whose protocol differs is refused">
                <span className="mono">{cloud.code.frozen_protocol_fingerprint}</span>{" "}
                <Yes value={cloud.code.matches_official_cohort} yes="the cohort's" no="not the cohort's" />
              </Row>
              <Row label="What it writes">
                <span className="mono">
                  {Object.entries(cloud.cohorts)
                    .map(([mode, binding]) => `${mode}: ${binding.root}/${binding.cohort_id}`)
                    .join("  ·  ")}
                </span>
              </Row>
              <Row label="Next prospective S0">
                <span className="mono">{cloud.phase_b.next_s0}</span>{" "}
                <span className="hint">
                  window {cloud.phase_b.window_open ? "open" : "closed"} · opens {fmtJstAgo(cloud.phase_b.opens_at, ops.now)}
                </span>
              </Row>
              <Row label="Model requests">
                <span className="hint">{cloud.phase_b.jev}</span>
              </Row>
            </tbody>
          </table>
        ) : (
          <Missing what="the cloud" />
        )}
      </Panel>

      <Panel title="Checks">
        <PublishedAt what="Run against this deployment before the first day," at={pc?.checks.before_the_deployment.at} />
        <table>
          <tbody>
            <Row label="Extensions in a step" hint="curl_cffi and tiktoken inside the Workflow sandbox">
              <Yes value={(checks.A?.ok as boolean) ?? null} yes="pass" no="fail" />{" "}
              <span className="hint">
                {checks.A ? `${checks.A.bars ?? "—"} bars, crumb ${String(checks.A.crumb_obtained)}, ` +
                  `${checks.A.o200k_harmony_tokens ?? "—"} o200k tokens` : "not run"}
              </span>
            </Row>
            <Row label="One security" hint="the day's own read path, end to end">
              <Yes value={(checks.B?.ok as boolean) ?? null} yes="pass" no="fail" />{" "}
              <span className="hint">{measure(checks.B)}</span>
            </Row>
            <Row label="Ten securities" hint="four-digit and new alphanumeric codes, liquid and thin, split and not">
              <Yes value={(checks.C?.ok as boolean) ?? null} yes="pass" no="fail" />{" "}
              <span className="hint">{measure(checks.C)}</span>
            </Row>
            <Row label="A day's read step" hint="150 securities, as production reads them">
              <Yes value={(checks.D?.ok as boolean) ?? null} yes="pass" no="fail" />{" "}
              <span className="hint">{measure(checks.D)}</span>
              {checks.D?.fits_the_function_limit ? (
                <div className="hint">
                  {JSON.stringify(checks.D.fits_the_function_limit).replace(/[{}"]/g, "").replace(/,/g, " · ")}
                </div>
              ) : null}
            </Row>
            <Row label="Scheduled cron" hint="Vercel's own schedule, not a manual call">
              {cron ? <span className="tag ok">verified</span> : <span className="tag warn">not observed</span>}{" "}
              <span className="mono">{cron ? fmtJstAgo(cron, ops.now) : "—"}</span>
              <div className="hint">
                cron_verified_at is the newest cloud record whose trigger was a cron; until one is written, the
                daily schedule is an open item of the move to the cloud.
              </div>
            </Row>
            <Row label="Rehearsal" hint="the whole pipeline on a past day, in its own cohort">
              <span className="mono">{String(rehearsal.s0 ?? "—")}</span>{" "}
              {rehearsal.status ? <span className="tag">{String(rehearsal.status)}</span> : null}{" "}
              <Link href="/runs">runs</Link>
            </Row>
          </tbody>
        </table>
      </Panel>

      <Panel title="Free quota">
        <PublishedAt what="Read from the Vercel dashboard, which has no API," at={String(usage.read_at ?? "")}
                     by={String(usage.read_by ?? "")} />
        {pc ? (
          <table>
            <tbody>
              <Row label="Functions Storage" hint="every deployment's functions, team-wide">
                <Quota used={`${usage.functions_storage?.used_gb ?? "—"} GB`}
                       limit={`${usage.functions_storage?.limit_gb ?? "—"} GB`}
                       over={Boolean(usage.functions_storage?.over)} />
              </Row>
              <Row label="Deployment Storage">
                <Quota used={`${usage.deployment_storage?.used_gb ?? "—"} GB`}
                       limit={`${usage.deployment_storage?.limit_gb ?? "—"} GB`} />
              </Row>
              <Row label="Fluid Active CPU">
                <Quota used={String(usage.fluid_active_cpu?.used ?? "—")}
                       limit={String(usage.fluid_active_cpu?.limit ?? "—")} />
              </Row>
              <Row label="Blob">
                <span className="mono">
                  {String(usage.blob?.storage_mb ?? "—")} MB · {String(usage.blob?.simple_operations ?? "—")} simple ·{" "}
                  {String(usage.blob?.advanced_operations ?? "—")} advanced of {String(usage.blob?.advanced_limit ?? "—")}
                </span>
              </Row>
              <Row label="Workflows">
                <span className="mono">
                  events {String(usage.workflows?.events ?? "—")} · data written {String(usage.workflows?.data_written ?? "—")}
                </span>
              </Row>
              <Row label="Note"><span className="hint">{String(usage.note ?? "")}</span></Row>
            </tbody>
          </table>
        ) : (
          <Missing what="the quota" />
        )}
      </Panel>

      <Panel title="The official Phase B, on the operator's machine">
        <PublishedAt what="From that machine," at={pc?.published_at} by={pc?.publisher} />
        {pc ? (
          <table>
            <tbody>
              <Row label="Status">
                <span className="tag">{pc.phase_b.status}</span>{" "}
                <span className="hint">
                  {pc.phase_b.days.count} days written · {pc.phase_b.days.sent.length} sent ·{" "}
                  {pc.phase_b.cohort_stopped.length ? `stopped: ${pc.phase_b.cohort_stopped.join(", ")}` : "not stopped"}
                </span>
              </Row>
              <Row label="Cohort">
                <span className="mono">{pc.phase_b.cohort_id}</span>{" "}
                <span className="hint">{pc.phase_b.evaluation_version} · created {pc.phase_b.created_at.slice(0, 10)}</span>
              </Row>
              <Row label="Frozen protocol">
                <span className="mono">{pc.phase_b.frozen_protocol_fingerprint}</span>{" "}
                <Yes value={pc.phase_b.frozen_protocol_fingerprint === cloud?.code.frozen_protocol_fingerprint}
                     yes="same as the cloud's" no="differs from the cloud's" />
              </Row>
              <Row label="First eligible S0">
                <span className="mono">{pc.phase_b.first_eligible_s0}</span>{" "}
                <span className="hint">{pc.phase_b.target_business_days} business days are the target</span>
              </Row>
              <Row label="Scheduled tasks">
                {pc.phase_b.scheduler.length ? (
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {pc.phase_b.scheduler.map((task) => (
                      <li key={task.task} className="mono">
                        {task.task} — {task.state}, next {task.next_run || "—"}, last {task.last_run || "—"}{" "}
                        (result {task.last_result})
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="mono">—</span>
                )}
              </Row>
              <Row label="Frozen worktree">
                <span className="mono">{pc.phase_b.frozen_worktree.commit?.slice(0, 12) ?? "—"}</span>{" "}
                <Yes value={pc.phase_b.frozen_worktree.clean} yes="clean" no="modified" />
                <div className="hint">{pc.phase_b.frozen_worktree.path}</div>
              </Row>
              <Row label="TypeSafe credit">
                <span className="mono">${pc.phase_b.credit.confirmed_balance_usd ?? "—"}</span>{" "}
                <span className="hint">
                  confirmed {pc.phase_b.credit.confirmed_at?.slice(0, 10) ?? "—"} · expires{" "}
                  {pc.phase_b.credit.displayed_expiry ?? "—"}
                </span>
              </Row>
              <Row label="Budget">
                <span className="mono">
                  ${pc.phase_b.spend.spent_usd ?? "0.00"} of ${pc.phase_b.budget.global_hard_cap_usd} spent
                </span>
                <div className="hint">
                  ${pc.phase_b.budget.daily_budget_usd} a day, at most {pc.phase_b.per_day.max_requests} requests ·{" "}
                  {pc.phase_b.pacing}
                </div>
              </Row>
              <Row label="Send window"><span className="hint">{pc.phase_b.send_window}</span></Row>
            </tbody>
          </table>
        ) : (
          <Missing what="the PC" />
        )}
      </Panel>
    </>
  );
}

function measure(check: Record<string, unknown> | undefined): string {
  if (!check) return "not run";
  const m = (check.measure ?? {}) as Record<string, number>;
  const parts = [
    m.read !== undefined ? `${m.read}/${m.attempted} read` : null,
    m.failed !== undefined ? `${m.failed} failed` : null,
    m.seconds !== undefined ? `${m.seconds}s` : null,
    m.cpu_seconds !== undefined ? `${m.cpu_seconds}s CPU` : null,
    m.max_rss_mb !== undefined ? `${m.max_rss_mb} MB peak` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "ran";
}
