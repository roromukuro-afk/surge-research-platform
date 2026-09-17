/** Small shared pieces. Server components: nothing here needs interactivity. */

import { isConfigured } from "@/lib/db";

export function NotConfigured() {
  return (
    <div className="empty">
      <p style={{ marginTop: 0 }}>
        <strong>No database connection configured.</strong>
      </p>
      <p>
        Set <code>SURGE_WEB_DATABASE_URL</code> in the server environment to a connection string for a
        login role granted <code>surge_web</code>. Deliberately not the worker credential and not{" "}
        <code>surge_readonly</code>, which can read the base tables: these screens read the{" "}
        <code>ui</code> contracts and nothing else.
      </p>
      <p style={{ marginBottom: 0 }}>
        The value belongs in the environment, not in the repository.
      </p>
    </div>
  );
}

export function Guard({ children }: { children: React.ReactNode }) {
  if (!isConfigured()) return <NotConfigured />;
  return <>{children}</>;
}

export function Empty({ what, why }: { what: string; why?: string }) {
  return (
    <div className="empty">
      <strong>No {what} yet.</strong>
      {why ? <p style={{ margin: "6px 0 0" }}>{why}</p> : null}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

/**
 * Money and ratios arrive from Postgres as strings so they do not pass through a
 * float on the way here. Formatting is the only place they become numbers, and
 * only for display.
 */
export function Num({ value, digits = 2 }: { value: string | number | null; digits?: number }) {
  if (value === null || value === undefined || value === "") return <span className="mono">—</span>;
  const parsed = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(parsed)) return <span className="mono">{String(value)}</span>;
  return <span className="mono">{parsed.toLocaleString(undefined, { maximumFractionDigits: digits })}</span>;
}

export function When({ value }: { value: string | null }) {
  if (!value) return <span className="mono">—</span>;
  return <span className="mono">{new Date(value).toISOString().replace("T", " ").slice(0, 16)}</span>;
}

export function Tag({ tone, children }: { tone?: "ok" | "warn" | "stop"; children: React.ReactNode }) {
  return <span className={tone ? `tag ${tone}` : "tag"}>{children}</span>;
}

export async function FailedToRead({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="notice stop">
      <strong>Could not read the contracts.</strong> {message}
    </div>
  );
}
