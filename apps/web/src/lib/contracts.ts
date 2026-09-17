/**
 * The shape of the `ui` schema, mirrored in TypeScript.
 *
 * These are contracts, not conveniences: the column names come from the views in
 * `supabase/migrations/20260917180000_ui_read_contracts.sql`, and renaming one on
 * either side without the other is a break. When a view changes, this file
 * changes in the same commit.
 *
 * Numeric columns arrive as strings. `pg` returns Postgres `numeric` as a string
 * so that a price like 1234.56 does not silently become a float somewhere between
 * the database and the screen. Formatting happens at render time; nothing here
 * converts money to a number.
 */

export type MarketCode = "JP" | "US";

/** Mirrors `analysis.stage3_state`. There is deliberately no ENTRY member. */
export type Stage3State =
  | "TECHNICAL_SETUP_EOD"
  | "POST_CLOSE_CATALYST_SETUP"
  | "WATCH_BREAKOUT"
  | "WATCH_PULLBACK"
  | "WATCH_OTHER"
  | "REJECT";

/** Mirrors `analysis.zone_basis_kind`. PRIOR_HIGH is not a member, by design. */
export type ZoneBasisKind =
  | "CURRENT_MATERIAL"
  | "SUPPLY_STRUCTURE"
  | "VOLUME_STRUCTURE"
  | "SUPPORT_RESISTANCE"
  | "VOLATILITY_RANGE"
  | "SECTOR_MOVE";

export type PriceDecision =
  | "PRICE_ELIGIBLE"
  | "PRICE_ABOVE_3000"
  | "PRICE_MISSING"
  | "FX_MISSING"
  | "STALE_PRICE"
  | "STALE_FX";

export type ObstacleKind =
  | "PRIOR_SURGE_HIGH"
  | "SUPPLY_OVERHANG"
  | "HORIZONTAL_RESISTANCE"
  | "MOVING_AVERAGE"
  | "ROUND_NUMBER"
  | "GAP_EDGE"
  | "VWAP_ANCHOR";

/** `ui.dashboard_daily` */
export interface DashboardRow {
  as_of_date: string;
  market_code: MarketCode;
  price_eligible: number;
  stale_inputs: number;
  technical_candidates: number;
  material_candidates: number;
  technical_setups: number;
  catalyst_setups: number;
  watching: number;
  rejected: number;
  failed_validation: number;
  /** How many verdicts came from the deterministic stand-in rather than a model. */
  from_the_stand_in: number;
}

/** `ui.universe_eligibility` */
export interface UniverseRow {
  as_of_date: string;
  market_code: MarketCode;
  security_id: string;
  native_symbol: string;
  price_decision: PriceDecision;
  price: string | null;
  price_currency: string | null;
  price_jpy: string | null;
  fx_rate: string | null;
  fx_age_seconds: number | null;
  price_age_days: number | null;
  rule_version: string;
  universe_decision: string | null;
  knowledge_cutoff: string;
}

/** `ui.materials_as_of(timestamptz)` */
export interface MaterialRow {
  event_id: string;
  event_key: string;
  event_type: string;
  headline: string | null;
  scope: string;
  first_known_at: string;
  occurred_at: string | null;
  occurred_at_precision: string;
  /** Distinct publishers in a discovering or verifying role. Reprints excluded. */
  independent_sources: string;
  corroborations: string;
  relevance: string | null;
  relevance_reason: string | null;
  securities: string;
  strongest_relation: string | null;
}

/** `ui.watch_and_setup` */
export interface WatchRow {
  as_of_date: string;
  security_id: string;
  state: Stage3State;
  rationale: string;
  confidence_note: string | null;
  /** Arithmetic on the reference price. Never the same thing as the zone. */
  twenty_percent_threshold_price: string | null;
  threshold_reference_price: string | null;
  threshold_reference_kind: string | null;
  reachable_zone_low: string | null;
  reachable_zone_high: string | null;
  reachable_zone_basis_kinds: ZoneBasisKind[];
  reachable_zone_basis: string | null;
  obstacles_considered: unknown;
  concepts_considered: string[];
  provider_kind: string;
  provider_id: string;
  validation_status: string;
  validation_errors: string[];
  knowledge_cutoff: string;
}

/** `ui.coverage_summary` */
export interface CoverageRow {
  domain: string;
  as_of_date: string | null;
  component: string;
  records: string;
  /** The provider failed us. */
  provider_errors: string;
  /** The data is thin in a way the provider has told us about. */
  quality_warnings: string;
  /** We failed to read what the provider sent. A different problem entirely. */
  our_parse_errors: string;
  quality: string;
}

/** `ui.unfilled_roles` */
export interface UnfilledRole {
  role: string;
  domain: string;
  detail: string;
}

/** `ui.pipeline_runs` */
export interface RunRow {
  run_id: string;
  job_name: string;
  job_version: string;
  run_mode: string;
  market_code: MarketCode | null;
  status: string;
  as_of_date: string | null;
  started_at: string;
  finished_at: string | null;
  data_cutoff: string | null;
  git_sha: string | null;
  config_hash: string | null;
  /** A finished run is not an authoritative run. Publication is what decides. */
  published: boolean;
  published_at: string | null;
}

/** `ui.not_live_verified` */
export interface NotLiveVerifiedRow {
  component: string;
  name: string;
  detail: string;
}

export interface PriceObstacle {
  kind: ObstacleKind;
  price_level: string;
  distance_pct: string | null;
  established_on: string | null;
  volume_at_level: string | null;
  touch_count: number | null;
  /** Why this level may no longer hold. Overhang expires. */
  weakening_evidence: string | null;
}

/** `ui.stock_detail(uuid, date)` */
export interface StockDetail {
  security_id: string;
  as_of_date: string;
  eligibility: {
    decision: PriceDecision;
    price: string | null;
    price_currency: string | null;
    converted_jpy: string | null;
    fx_age_seconds: number | null;
    price_age_days: number | null;
    rule_version: string;
  } | null;
  features: Record<string, unknown> | null;
  technical_routes: string[] | null;
  material_routes: {
    routes: string[];
    event_ids: string[];
    strongest_relation: string | null;
  } | null;
  stage2: Record<string, unknown> | null;
  /** Levels in the way. Never rendered as targets. */
  price_obstacles: PriceObstacle[];
  stage3: Record<string, unknown> | null;
}

export const STATE_LABELS: Record<Stage3State, string> = {
  TECHNICAL_SETUP_EOD: "Technical setup",
  POST_CLOSE_CATALYST_SETUP: "Post-close catalyst",
  WATCH_BREAKOUT: "Watching for a breakout",
  WATCH_PULLBACK: "Watching a pullback",
  WATCH_OTHER: "Watching",
  REJECT: "Rejected",
};

export const DECISION_LABELS: Record<PriceDecision, string> = {
  PRICE_ELIGIBLE: "Eligible",
  PRICE_ABOVE_3000: "Above 3,000 JPY",
  PRICE_MISSING: "No price",
  FX_MISSING: "No FX rate",
  STALE_PRICE: "Price too old to use",
  STALE_FX: "FX rate too old to use",
};

/**
 * Mirrors `prod.entry_attempt_status`. Six of the seven produce no prediction,
 * and all seven are in the same view on purpose: the ones that produced nothing
 * are the denominator of any honest hit rate.
 */
export type EntryAttemptStatus =
  | "PREDICTION_CREATED"
  | "ENTRY_ABORTED_PRICE_LIMIT"
  | "REJECTED_HARD_FILTER_AT_DECISION"
  | "REJECTED_BY_ANALYSIS"
  | "REJECTED_NOT_IN_UNIVERSE"
  | "REAFFIRMED_EXISTING_EPISODE"
  | "NO_ENTRY_REFERENCE_PRICE";

export const ATTEMPT_LABELS: Record<EntryAttemptStatus, string> = {
  PREDICTION_CREATED: "Entered",
  ENTRY_ABORTED_PRICE_LIMIT: "Aborted at the limit",
  REJECTED_HARD_FILTER_AT_DECISION: "Over 3,000 yen",
  REJECTED_BY_ANALYSIS: "Analysis said no",
  REJECTED_NOT_IN_UNIVERSE: "Outside the universe",
  REAFFIRMED_EXISTING_EPISODE: "Reaffirmed",
  NO_ENTRY_REFERENCE_PRICE: "No tradeable price",
};

/** Mirrors `ui.open_episodes`. */
export type OpenEpisodeRow = {
  episode_id: string;
  security_id: string;
  thesis_key: string;
  opened_at: string;
  entry_price_observed_at: string;
  horizon_sessions: number;
  entry_reference_price: string;
  entry_price_currency: string;
  target_price: string;
  /** Fixed at creation. What the outcome is judged against. */
  initial_failure_line: string;
  /** May move on reanalysis, and never closes an episode. */
  current_risk_line: string | null;
  provider_id: string;
  verification: string;
  transition_count: string;
};

/** Mirrors `ui.entry_attempt_ledger`. */
export type EntryAttemptRow = {
  attempt_id: string;
  security_id: string;
  status: EntryAttemptStatus;
  analysis_kind: string;
  decision_completed_at: string;
  decision_price_jpy: string | null;
  entry_price_jpy: string | null;
  universe_decision: string | null;
  reject_reason: string | null;
  verification: string;
  produced_a_prediction: boolean;
};
