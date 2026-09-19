/**
 * Where the Phase B shadow's artifacts are read from (D-279).
 *
 * Three sources, one format (workers/src/surge/shadow/artifacts.py):
 *
 * - `fixture`: the synthetic cohort committed under `fixtures/shadow`
 *   (scripts/make_shadow_fixture.py). Made-up data; every page says so.
 * - `local:<dir>`: a directory in the same layout, e.g. an export of a cohort
 *   the PC wrote, for comparing on this machine. Never on Vercel.
 * - `blob`: the private Vercel Blob store the shadow writes to. The token is
 *   `BLOB_READ_WRITE_TOKEN`, read by `@vercel/blob` from the server's
 *   environment; it never reaches the browser, and nothing here logs it.
 *
 * `SURGE_SHADOW_SOURCE` picks one. Unset, development uses the fixture and
 * production uses nothing: a deployment without a store says it is not
 * configured rather than quietly showing made-up numbers.
 *
 * Blob is billed per operation, and on Hobby a team that runs out loses Blob
 * for 30 days, so reads are cached: an object is written once and never
 * changes, so a hit is kept for good. A miss is asked again after 15 minutes,
 * or after a day when the caller knows nothing more can arrive under that key
 * (`settled`: a run record of a day long past). Nothing here lists the store
 * (a list is an advanced operation): every key is derived from the calendar
 * and the cohort, and keys that cannot exist yet are not asked for at all.
 */

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { gunzipSync } from "node:zlib";
import { unstable_cache } from "next/cache";

export type SourceKind = "fixture" | "local" | "blob";

export interface ArtifactSource {
  kind: SourceKind;
  /** For the banner: what the reader is looking at. Never a credential. */
  description: string;
  /**
   * The object's bytes, or null when the key holds nothing. `settled`: nothing
   * can be written under this key any more, so a miss may be remembered longer.
   */
  read(key: string, options?: ReadOptions): Promise<Buffer | null>;
}

export interface ReadOptions {
  settled?: boolean;
}

const KEY = /^[A-Za-z0-9][A-Za-z0-9._-]*(\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/;

function checkKey(key: string): string {
  if (!KEY.test(key) || key.split("/").some((part) => part === "." || part === "..")) {
    throw new Error(`not an artifact key: ${JSON.stringify(key)}`);
  }
  return key;
}

export const FIXTURE_ROOT = path.join(process.cwd(), "fixtures", "shadow");

class DirectorySource implements ArtifactSource {
  constructor(
    readonly kind: SourceKind,
    private readonly root: string,
    readonly description: string,
  ) {}

  async read(key: string): Promise<Buffer | null> {
    const target = path.join(this.root, ...checkKey(key).split("/"));
    try {
      return await readFile(target);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  }
}

async function blobRead(key: string): Promise<Buffer | null> {
  const { get } = await import("@vercel/blob");
  const result = await get(key, { access: "private" });
  if (!result) return null;
  if (result.statusCode !== 200) throw new Error(`Blob answered ${result.statusCode} for ${key}`);
  return Buffer.from(await new Response(result.stream).arrayBuffer());
}

class NotYet extends Error {}

/** Found once, found for good: an object is never rewritten. */
const blobFound = unstable_cache(
  async (key: string): Promise<string> => {
    const data = await blobRead(key);
    if (data === null) throw new NotYet(key); // not cached: a miss is asked again
    return data.toString("base64");
  },
  ["surge-shadow-blob-found"],
  { revalidate: false, tags: ["surge-shadow"] },
);

async function foundOrNull(key: string): Promise<string | null> {
  try {
    return await blobFound(key);
  } catch (error) {
    if (error instanceof NotYet) return null;
    throw error;
  }
}

/** A miss is remembered for 15 minutes, so a page view is not a Blob operation per missing key. */
const blobRecent = unstable_cache(foundOrNull, ["surge-shadow-blob-recent"], {
  revalidate: 900,
  tags: ["surge-shadow"],
});

/** A miss under a key nothing can be written to any more, remembered for a day. */
const blobSettled = unstable_cache(foundOrNull, ["surge-shadow-blob-settled"], {
  revalidate: 86_400,
  tags: ["surge-shadow"],
});

class BlobSource implements ArtifactSource {
  readonly kind = "blob" as const;
  readonly description = "the shadow's private Vercel Blob store";

  async read(key: string, options?: ReadOptions): Promise<Buffer | null> {
    const encoded = await (options?.settled ? blobSettled : blobRecent)(checkKey(key));
    return encoded === null ? null : Buffer.from(encoded, "base64");
  }
}

export type SourceSetting =
  | { source: ArtifactSource }
  | { source: null; reason: string };

export function shadowSource(): SourceSetting {
  const setting =
    process.env.SURGE_SHADOW_SOURCE ?? (process.env.NODE_ENV === "development" ? "fixture" : "");
  if (setting === "fixture") {
    return {
      source: new DirectorySource("fixture", FIXTURE_ROOT, "the synthetic fixture (made-up data)"),
    };
  }
  if (setting.startsWith("local:")) {
    if (process.env.VERCEL) return { source: null, reason: "a local directory cannot be read on Vercel" };
    return { source: new DirectorySource("local", setting.slice("local:".length), "a local export") };
  }
  if (setting === "blob") {
    if (!process.env.BLOB_READ_WRITE_TOKEN) {
      return { source: null, reason: "SURGE_SHADOW_SOURCE is blob, but no Blob store is connected" };
    }
    return { source: new BlobSource() };
  }
  return {
    source: null,
    reason: setting
      ? `SURGE_SHADOW_SOURCE=${JSON.stringify(setting)} is not a source (fixture, local:<dir> or blob)`
      : "SURGE_SHADOW_SOURCE is not set",
  };
}

// ----------------------------------------------------------------- integrity

export interface ArtifactEntry {
  sha256: string;
  bytes: number;
  content_type: string;
  content_sha256?: string;
}

export interface CommitRecord {
  schema: string;
  prefix: string;
  committed_at: string;
  artifacts: Record<string, ArtifactEntry>;
  [field: string]: unknown;
}

export const SCHEMA = "phase-b-shadow-artifacts-1";

export class IntegrityError extends Error {}

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");

export async function readJson<T>(source: ArtifactSource, key: string, options?: ReadOptions): Promise<T | null> {
  const data = await source.read(key, options);
  return data === null ? null : (JSON.parse(data.toString("utf-8")) as T);
}

export async function readCommit(
  source: ArtifactSource,
  prefix: string,
  name: string,
  options?: ReadOptions,
): Promise<CommitRecord | null> {
  const record = await readJson<CommitRecord>(source, `${prefix}/${name}`, options);
  if (record && record.schema !== SCHEMA) {
    throw new IntegrityError(`${prefix}/${name}: schema ${String(record.schema)}, expected ${SCHEMA}`);
  }
  return record;
}

/** An artifact's text, checked against its commit record; gzip objects come back decompressed. */
export async function readVerified(
  source: ArtifactSource,
  commit: CommitRecord,
  name: string,
): Promise<string | null> {
  const entry = commit.artifacts[name];
  if (!entry) return null;
  const data = await source.read(`${commit.prefix}/${name}`);
  if (data === null) throw new IntegrityError(`${commit.prefix}/${name} is named by its commit record but missing`);
  if (data.length !== entry.bytes || sha256(data) !== entry.sha256) {
    throw new IntegrityError(`${commit.prefix}/${name} does not match its commit record`);
  }
  if (entry.content_type === "application/gzip") {
    const content = gunzipSync(data);
    if (sha256(content) !== entry.content_sha256) {
      throw new IntegrityError(`${commit.prefix}/${name}: the content does not match its commit record`);
    }
    return content.toString("utf-8");
  }
  return data.toString("utf-8");
}

export function jsonLines<T>(text: string | null): T[] {
  if (!text) return [];
  return text
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as T);
}
