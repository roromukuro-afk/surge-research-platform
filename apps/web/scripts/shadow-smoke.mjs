/**
 * Renders every Phase B shadow screen from the synthetic fixture, the way a
 * browser asks for it, against the production build (`next start`).
 *
 * The screens read artifacts at request time and check each one against its
 * integrity record, so a build that compiles can still fail on a page: a field
 * renamed on the Python side, a fixture file the build did not trace, an
 * artifact that no longer matches its record. This catches those in CI.
 *
 *   npm run build && node scripts/shadow-smoke.mjs
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PORT = Number(process.env.SHADOW_SMOKE_PORT ?? 3199);
const PAGES = [
  // The operational screens: with a fixture directory and no published records, they say so rather than guess.
  { path: "/", expect: ["Phase B is prospective", "The rehearsal", "/demo"] },
  { path: "/system", expect: ["This deployment", "Free quota", "Nothing published"] },
  { path: "/runs", expect: ["shadow day", "rehearsal", "compare"] },
  { path: "/compare", expect: ["The PC and the cloud, compared", "selected_history_digests", "request_hashes"] },
  { path: "/demo", expect: ["Made-up data", "synthetic-phase-b-fixture", "Predictions"] },
  // The cohort's own screens, on the synthetic cohort the fixture holds.
  { path: "/cohort?cohort=demo", expect: ["Synthetic fixture", "Business days", "awaiting outcomes", "73cceb0db1be4e3d"] },
  { path: "/cohort/predictions?s0=2026-09-24&cohort=demo", expect: [" — S0 2026-09-24", "anonymized", "jev-1.13.0"] },
  { path: "/cohort/days?cohort=demo", expect: ["Scheduled invocations", "below 95%", "closed: 敬老の日"] },
  { path: "/cohort/outcomes?s0=2026-09-24&cohort=demo", expect: [" — S0 2026-09-24", "RESOLVED", "Awaiting T+20"] },
  { path: "/cohort/reports?cohort=demo", expect: ["report-2.json", "Route D subgroups", "partial"] },
  { path: "/cohort/system?cohort=demo", expect: ["Frozen protocol", "e500a1c57a343e3a", "not configured"] },
];
const FORBIDDEN = ["canonical_v5_1", "Could not read the shadow", "Application error"];

const server = spawn(process.execPath, [path.join(here, "node_modules", "next", "dist", "bin", "next"), "start", "-p", String(PORT)], {
  cwd: here,
  env: { ...process.env, SURGE_SHADOW_SOURCE: "fixture", TYPESAFE_API_KEY: "" },
  stdio: ["ignore", "pipe", "pipe"],
});
let log = "";
server.stdout.on("data", (chunk) => (log += chunk));
server.stderr.on("data", (chunk) => (log += chunk));

async function ready() {
  for (let i = 0; i < 120; i++) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/system`);
      if (response.status < 500) return;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`next start did not answer on ${PORT}\n${log}`);
}

let failures = 0;
try {
  await ready();
  for (const page of PAGES) {
    const response = await fetch(`http://127.0.0.1:${PORT}${page.path}`);
    const html = await response.text();
    const missing = page.expect.filter((text) => !html.includes(text));
    const found = FORBIDDEN.filter((text) => html.includes(text));
    const ok = response.status === 200 && missing.length === 0 && found.length === 0;
    if (!ok) failures++;
    console.log(
      `${ok ? "ok  " : "FAIL"} ${page.path} ${response.status}` +
        (missing.length ? ` missing ${JSON.stringify(missing)}` : "") +
        (found.length ? ` must not contain ${JSON.stringify(found)}` : ""),
    );
  }
} finally {
  server.kill();
}
if (failures) {
  console.error(`${failures} page(s) failed\n${log.slice(-4000)}`);
  process.exit(1);
}
console.log(`all ${PAGES.length} shadow pages render from the fixture`);
