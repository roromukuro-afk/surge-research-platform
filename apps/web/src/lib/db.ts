/**
 * The database connection, and the one rule it enforces.
 *
 * The application connects as `surge_web`, which holds USAGE on the `ui` schema
 * and SELECT on its views and nothing else. Deliberately not `surge_readonly`:
 * that role can read 79 base tables, which is right for a researcher at a psql
 * prompt and wrong for a web process. Under `surge_web` a page that reaches past
 * the contracts gets a permission error rather than a row, so the as-of filtering
 * and the replay-column exclusion baked into the views are enforced rather than
 * merely available.
 *
 * The connection string lives in the server environment and never reaches the
 * browser. Nothing in this file is importable from a client component.
 */

import { Pool } from "pg";
import type { QueryResultRow } from "pg";

declare global {
  // Next.js reloads modules in development; without this the pool is recreated
  // on every edit and the connection count climbs until the database refuses.
  // eslint-disable-next-line no-var
  var __surgePool: Pool | undefined;
}

export class MissingDatabaseUrl extends Error {
  constructor() {
    super(
      "SURGE_WEB_DATABASE_URL is not set. The web application needs a connection string " +
        "for a login role granted surge_web. That role reads the ui contracts and nothing " +
        "else: not the worker credential, not surge_readonly, and never the owner.",
    );
    this.name = "MissingDatabaseUrl";
  }
}

function pool(): Pool {
  const connectionString = process.env.SURGE_WEB_DATABASE_URL;
  if (!connectionString) throw new MissingDatabaseUrl();

  if (!global.__surgePool) {
    global.__surgePool = new Pool({
      connectionString,
      max: 4,
      idleTimeoutMillis: 10_000,
      // The pages are read-only by design; making the session read-only too
      // means a stray write is refused by the server rather than by good luck.
      options: "-c default_transaction_read_only=on",
    });
  }
  return global.__surgePool;
}

export async function query<T extends QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await pool().query<T>(sql, params);
  return result.rows;
}

export async function queryOne<T extends QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T | null> {
  const rows = await query<T>(sql, params);
  return rows[0] ?? null;
}

/** Whether the application is configured at all, for a friendly empty state. */
export function isConfigured(): boolean {
  return Boolean(process.env.SURGE_WEB_DATABASE_URL);
}
