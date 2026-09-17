/**
 * Reads every `ui` contract the way the application reads it.
 *
 * The failure this catches is specific and quiet: a view renamed or a grant
 * missed, so a screen that worked in development returns a permission error in
 * production. Building the app does not catch it - the SQL is a string until it
 * runs - so CI runs this against a real database with the migrations applied and
 * connects as the read-only principal the application uses.
 *
 * It also checks that the connection really is read-only, because "the
 * application only reads" is a claim about the credential, not about the code.
 */

import pg from "pg";

const CONTRACTS = [
  { name: "ui.dashboard_daily", sql: "select * from ui.dashboard_daily limit 1" },
  { name: "ui.universe_eligibility", sql: "select * from ui.universe_eligibility limit 1" },
  { name: "ui.watch_and_setup", sql: "select * from ui.watch_and_setup limit 1" },
  { name: "ui.coverage_summary", sql: "select * from ui.coverage_summary limit 1" },
  { name: "ui.unfilled_roles", sql: "select * from ui.unfilled_roles limit 1" },
  { name: "ui.pipeline_runs", sql: "select * from ui.pipeline_runs limit 1" },
  { name: "ui.not_live_verified", sql: "select * from ui.not_live_verified limit 1" },
  { name: "ui.open_episodes", sql: "select * from ui.open_episodes limit 1" },
  { name: "ui.entry_attempt_ledger", sql: "select * from ui.entry_attempt_ledger limit 1" },
  { name: "ui.episode_results", sql: "select * from ui.episode_results limit 1" },
  { name: "ui.outcome_counts", sql: "select * from ui.outcome_counts limit 1" },
  { name: "ui.teacher_label_counts", sql: "select * from ui.teacher_label_counts limit 1" },
  { name: "ui.teacher_data_summary", sql: "select * from ui.teacher_data_summary limit 1" },
  { name: "ui.miss_breakdown", sql: "select * from ui.miss_breakdown limit 1" },
  { name: "ui.route_performance", sql: "select * from ui.route_performance limit 1" },
  { name: "ui.material_driver_performance", sql: "select * from ui.material_driver_performance limit 1" },
  { name: "ui.concept_performance", sql: "select * from ui.concept_performance limit 1" },
  { name: "ui.llm_judgement_audit", sql: "select * from ui.llm_judgement_audit limit 1" },
  { name: "ui.model_version_comparison", sql: "select * from ui.model_version_comparison limit 1" },
  { name: "ui.walk_forward_results", sql: "select * from ui.walk_forward_results limit 1" },
  { name: "ui.calibration_diagnostics", sql: "select * from ui.calibration_diagnostics limit 1" },
  { name: "ui.promotion_audit", sql: "select * from ui.promotion_audit limit 1" },
  {
    name: "ui.materials_as_of",
    sql: "select * from ui.materials_as_of(clock_timestamp()) limit 1",
  },
  {
    name: "ui.stock_detail",
    sql: "select ui.stock_detail('00000000-0000-0000-0000-000000000000'::uuid, current_date)",
  },
];

/** Column names the pages destructure. A rename here is a break, not a tidy-up. */
const REQUIRED_COLUMNS = {
  "ui.dashboard_daily": [
    "as_of_date",
    "market_code",
    "price_eligible",
    "technical_candidates",
    "material_candidates",
    "from_the_stand_in",
  ],
  "ui.universe_eligibility": ["security_id", "native_symbol", "price_decision", "price_jpy", "rule_version"],
  "ui.watch_and_setup": [
    "state",
    "twenty_percent_threshold_price",
    "reachable_zone_high",
    "reachable_zone_basis_kinds",
    "provider_kind",
  ],
  "ui.coverage_summary": ["domain", "component", "provider_errors", "our_parse_errors", "quality_warnings"],
  "ui.unfilled_roles": ["role", "domain", "detail"],
  "ui.pipeline_runs": ["run_id", "job_name", "status", "published"],
  "ui.not_live_verified": ["component", "name", "detail"],
};

const connectionString = process.env.SURGE_WEB_DATABASE_URL;
if (!connectionString) {
  console.error("SURGE_WEB_DATABASE_URL is not set");
  process.exit(2);
}

const client = new pg.Client({ connectionString });
await client.connect();

let failures = 0;

for (const contract of CONTRACTS) {
  try {
    const result = await client.query(contract.sql);
    const required = REQUIRED_COLUMNS[contract.name];
    if (required) {
      const present = new Set(result.fields.map((field) => field.name));
      const missing = required.filter((column) => !present.has(column));
      if (missing.length) {
        console.error(`FAIL ${contract.name}: missing column(s) ${missing.join(", ")}`);
        failures += 1;
        continue;
      }
    }
    console.log(`ok   ${contract.name} (${result.fields.length} columns)`);
  } catch (error) {
    console.error(`FAIL ${contract.name}: ${error.message}`);
    failures += 1;
  }
}

// The application must not be able to write, whatever a page tries to do.
try {
  await client.query("create table ui.should_not_exist (x int)");
  console.error("FAIL the read-only principal was able to create a table");
  failures += 1;
} catch {
  console.log("ok   the read-only principal cannot write");
}

// And it must not be able to read past the contracts into a base table.
try {
  await client.query("select * from news.documents limit 1");
  console.error("FAIL the read-only principal can read base tables directly");
  failures += 1;
} catch {
  console.log("ok   the read-only principal cannot reach past the contracts");
}

await client.end();

if (failures) {
  console.error(`\n${failures} contract check(s) failed`);
  process.exit(1);
}
console.log("\nevery contract reads");
