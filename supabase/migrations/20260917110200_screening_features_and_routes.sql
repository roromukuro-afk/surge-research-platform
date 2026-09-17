-- Phase 3 Stage 1: numeric features, and Routes A-H as OR-type candidate generation.
--
-- Two decisions are baked into this schema.
--
-- The features are the record, not the pattern labels. "Volatility contraction"
-- is a name for a number; storing only the name discards the evidence and makes
-- a later threshold change unbacktestable. Every route therefore records the
-- measurements it fired on, and a candidate carries the list of routes that
-- found it rather than a single label.
--
-- Routes are ORed, not ANDed. A security reaches Stage 2 if ANY route finds it;
-- requiring all of them would produce an empty set and call it selectivity.
-- discovery_routes is an array for exactly that reason, and a name appearing in
-- it is a claim about which evidence fired, not a ranking.

create schema if not exists screening;
comment on schema screening is
  'Stage 1: daily numeric features over a comparable price series, and the Route A-H candidates derived from them.';

-- The privilege guards read this list. A schema missing from it is a schema
-- with no guard - which is how the market schema was briefly left open.
create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market', 'screening'];
$$;

grant usage on schema screening to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema screening
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema screening
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;
alter default privileges in schema screening
  grant execute on functions to current_user;
alter default privileges in schema screening
  revoke execute on functions from public;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'series_basis' and n.nspname = 'screening') then
    -- What the feature engine was fed. Vendor "adjusted" series are recomputed
    -- backwards on every new split, so a feature built on one is not stable
    -- across fetches; this project builds its own comparable series from raw
    -- prices and the corporate actions known at the as-of date.
    create type screening.series_basis as enum (
      'SPLIT_ADJUSTED_TO_AS_OF',   -- raw prices restated into the as-of date's share count
      'RAW_UNADJUSTED',            -- no adjustment applied; only valid where no action occurred
      'VENDOR_ADJUSTED'            -- the provider's own series. Recorded, not trusted.
    );
  end if;
end;
$$;

-- --------------------------------------------------------------- features
create table if not exists screening.features_daily (
  run_id uuid not null references pipeline.runs (run_id),
  market_code ref.market_code not null,
  provider_id text not null,
  native_symbol text not null,
  trade_date date not null,

  security_id uuid references ref.securities (security_id),
  identity_version text,
  feature_version text not null,
  series_basis screening.series_basis not null,

  bars_available integer not null,
  warmup_satisfied boolean not null,

  -- the bar itself, on the comparable series
  open numeric(20, 6),
  high numeric(20, 6),
  low numeric(20, 6),
  close numeric(20, 6),
  volume numeric(28, 6),
  turnover numeric(28, 4),

  -- returns
  ret_1d numeric(20, 8),
  ret_3d numeric(20, 8),
  ret_5d numeric(20, 8),
  ret_10d numeric(20, 8),
  ret_20d numeric(20, 8),

  -- extremes and distance to them
  high_5d numeric(20, 6),
  high_10d numeric(20, 6),
  high_20d numeric(20, 6),
  high_60d numeric(20, 6),
  low_5d numeric(20, 6),
  low_10d numeric(20, 6),
  low_20d numeric(20, 6),
  low_60d numeric(20, 6),
  dist_from_high_5d_pct numeric(20, 8),
  dist_from_high_10d_pct numeric(20, 8),
  dist_from_high_20d_pct numeric(20, 8),
  dist_from_high_60d_pct numeric(20, 8),
  dist_from_low_20d_pct numeric(20, 8),
  days_since_high_20d integer,
  days_since_high_60d integer,

  -- volatility
  atr_14 numeric(20, 6),
  atr_pct_14 numeric(20, 8),
  true_range_pct numeric(20, 8),
  rv_10d numeric(20, 8),
  rv_20d numeric(20, 8),
  rv_20d_annualized numeric(20, 8),

  -- volume and turnover
  vol_avg_5d numeric(28, 6),
  vol_avg_20d numeric(28, 6),
  vol_avg_60d numeric(28, 6),
  rvol_5d numeric(20, 8),
  rvol_20d numeric(20, 8),
  turnover_avg_20d numeric(28, 4),
  turnover_rvol numeric(20, 8),

  -- trend
  sma_5 numeric(20, 6),
  sma_25 numeric(20, 6),
  sma_75 numeric(20, 6),
  ema_12 numeric(20, 6),
  ema_26 numeric(20, 6),
  close_vs_sma25_pct numeric(20, 8),
  sma5_vs_sma25_pct numeric(20, 8),

  -- oscillators
  rsi_14 numeric(20, 8),
  macd numeric(20, 8),
  macd_signal numeric(20, 8),
  macd_hist numeric(20, 8),
  adx_14 numeric(20, 8),
  plus_di_14 numeric(20, 8),
  minus_di_14 numeric(20, 8),

  -- bands
  bb_mid_20 numeric(20, 6),
  bb_upper_20 numeric(20, 6),
  bb_lower_20 numeric(20, 6),
  bb_width_20 numeric(20, 8),
  bb_percent_b numeric(20, 8),

  -- intraday shape
  vwap_day numeric(20, 6),
  vwap_source text,
  gap_pct numeric(20, 8),
  body_pct numeric(20, 8),
  upper_wick_pct numeric(20, 8),
  lower_wick_pct numeric(20, 8),
  close_location_value numeric(20, 8),

  -- compression and breakout geometry
  range_5d_pct numeric(20, 8),
  range_20d_pct numeric(20, 8),
  range_contraction_ratio numeric(20, 8),
  breakout_distance_pct numeric(20, 8),
  consecutive_up_days integer,

  -- support / resistance primitives
  resistance_20d numeric(20, 6),
  resistance_60d numeric(20, 6),
  support_20d numeric(20, 6),
  support_60d numeric(20, 6),

  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  computed_at timestamptz not null default clock_timestamp(),

  primary key (run_id, provider_id, market_code, native_symbol, trade_date)
);

comment on table screening.features_daily is
  'The numeric record Stage 1 reasons over. Every value is derived from a comparable price series whose basis is named on the row, so a feature computed today and one computed next year mean the same thing.';
comment on column screening.features_daily.warmup_satisfied is
  'False when there were not enough bars for the longest window. The row is still written - a security with 30 bars has real 20-day features - and the flag says which of its longer features are null because of history rather than because of the market.';
comment on column screening.features_daily.vwap_source is
  'How vwap_day was obtained: TURNOVER_OVER_VOLUME where the provider supplies turnover, or null where it does not. Phase 2 has no intraday data, so a true session VWAP is not available and is not faked.';
comment on column screening.features_daily.series_basis is
  'What the engine was fed. VENDOR_ADJUSTED is recorded for comparison only: those series are recomputed retroactively and are not stable inputs.';

create index if not exists features_daily_security_idx
  on screening.features_daily (security_id, trade_date desc);
create index if not exists features_daily_date_idx
  on screening.features_daily (market_code, trade_date desc);

drop trigger if exists freeze_published_features on screening.features_daily;
create trigger freeze_published_features
  before insert or update or delete on screening.features_daily
  for each row execute function pipeline.freeze_published_run_artifacts();

-- ----------------------------------------------------------------- routes
create table if not exists screening.route_definitions (
  route_version text not null,
  route_code text not null,
  route_name text not null,
  description text not null,
  thresholds jsonb not null default '{}'::jsonb,
  enabled boolean not null default true,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  primary key (route_version, route_code)
);

comment on table screening.route_definitions is
  'Routes A-H and the numbers each one fires on, versioned. A threshold change is a new route_version so that candidates already recorded keep meaning what they meant.';

insert into screening.route_definitions (route_version, route_code, route_name, description, thresholds) values
  ('route-1.0.0', 'A', 'Breakout proximity',
   'Close is near a recent high with the range still intact - the setup before a break, not after it.',
   '{"max_dist_from_high_20d_pct": 3.0, "min_rvol_20d": 1.0, "min_bars": 25}'),
  ('route-1.0.0', 'B', 'Reclaim / trend reversal',
   'Price crosses back above a medium moving average after trading below it.',
   '{"lookback_below_days": 5, "min_close_vs_sma25_pct": 0.0, "min_bars": 30}'),
  ('route-1.0.0', 'C', 'Volume-leading',
   'Volume expands well ahead of price - interest arriving before the move.',
   '{"min_rvol_20d": 2.5, "max_abs_ret_1d_pct": 5.0, "min_bars": 25}'),
  ('route-1.0.0', 'D', 'Volatility contraction',
   'Recent range is a fraction of the prior range, and the bands have narrowed.',
   '{"max_range_contraction_ratio": 0.6, "max_bb_width_20": 0.10, "min_bars": 30}'),
  ('route-1.0.0', 'E', 'Seller exhaustion / reversal',
   'A long lower wick and a close near the high after a decline.',
   '{"min_lower_wick_pct": 40.0, "min_close_location_value": 0.6, "max_ret_5d_pct": -5.0, "min_bars": 10}'),
  ('route-1.0.0', 'F', 'Small-cap supply-demand elasticity',
   'Small turnover base with a sharp relative expansion - where a modest flow moves the price.',
   '{"max_turnover_avg_20d_jpy": 500000000, "max_turnover_avg_20d_usd": 3500000, "min_turnover_rvol": 3.0, "min_bars": 25}'),
  ('route-1.0.0', 'G', 'Early momentum',
   'A fresh multi-day advance from a quiet base rather than an extended run.',
   '{"min_ret_5d_pct": 5.0, "max_ret_20d_pct": 30.0, "min_rvol_20d": 1.5, "min_bars": 25}'),
  ('route-1.0.0', 'H', 'Healthy pullback',
   'An established uptrend giving back part of the move on falling volume.',
   '{"min_close_vs_sma25_pct": 0.0, "min_dist_from_high_20d_pct": 3.0, "max_dist_from_high_20d_pct": 15.0, "max_rvol_5d": 1.0, "min_bars": 30}')
on conflict (route_version, route_code) do nothing;

create table if not exists screening.route_candidates (
  run_id uuid not null references pipeline.runs (run_id),
  market_code ref.market_code not null,
  provider_id text not null,
  native_symbol text not null,
  trade_date date not null,

  security_id uuid references ref.securities (security_id),
  route_version text not null,
  feature_version text not null,

  discovery_routes text[] not null,
  route_evidence jsonb not null default '{}'::jsonb,
  route_count integer not null,

  price_eligible boolean,
  universe_decision universe.decision,

  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  computed_at timestamptz not null default clock_timestamp(),

  primary key (run_id, provider_id, market_code, native_symbol, trade_date),
  constraint route_candidates_routes_ck check (cardinality(discovery_routes) = route_count),
  constraint route_candidates_nonempty_ck check (route_count > 0)
);

comment on table screening.route_candidates is
  'A security that at least one route found, and which routes found it. Routes are ORed: the array is the answer, not a tiebreak.';
comment on column screening.route_candidates.route_evidence is
  'The measurements each firing route fired on, keyed by route code. Without this a candidate is an assertion; with it, a later threshold change can be evaluated against history instead of re-run blind.';
comment on column screening.route_candidates.price_eligible is
  'Whether the 3,000 JPY filter passed for this security on this date. Candidates are generated over the priced universe and this records the filter''s answer alongside, rather than silently dropping the row.';

create index if not exists route_candidates_security_idx
  on screening.route_candidates (security_id, trade_date desc);
create index if not exists route_candidates_routes_idx
  on screening.route_candidates using gin (discovery_routes);

drop trigger if exists freeze_published_route_candidates on screening.route_candidates;
create trigger freeze_published_route_candidates
  before insert or update or delete on screening.route_candidates
  for each row execute function pipeline.freeze_published_run_artifacts();

create or replace view screening.route_coverage as
  select run_id,
         market_code,
         trade_date,
         route_version,
         count(*) as candidates,
         count(*) filter (where price_eligible) as price_eligible,
         count(*) filter (where 'A' = any (discovery_routes)) as route_a,
         count(*) filter (where 'B' = any (discovery_routes)) as route_b,
         count(*) filter (where 'C' = any (discovery_routes)) as route_c,
         count(*) filter (where 'D' = any (discovery_routes)) as route_d,
         count(*) filter (where 'E' = any (discovery_routes)) as route_e,
         count(*) filter (where 'F' = any (discovery_routes)) as route_f,
         count(*) filter (where 'G' = any (discovery_routes)) as route_g,
         count(*) filter (where 'H' = any (discovery_routes)) as route_h,
         count(*) filter (where route_count > 1) as multi_route
  from screening.route_candidates
  group by run_id, market_code, trade_date, route_version;

comment on view screening.route_coverage is
  'How many candidates each route produced. A route that fires on everything and a route that never fires are both broken, and both are visible here.';

-- ---------------------------------------------------------------- privileges
revoke all on table
  screening.features_daily, screening.route_definitions, screening.route_candidates from public;
grant select on table
  screening.features_daily, screening.route_definitions, screening.route_candidates
to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;
grant insert on table screening.features_daily, screening.route_candidates to surge_worker_prod;
grant delete on table screening.features_daily, screening.route_candidates to surge_purge;
grant select on screening.route_coverage
  to surge_worker_prod, surge_worker_research, surge_readonly;

grant execute on function pipeline.project_schemas()
  to surge_worker_prod, surge_worker_research, surge_readonly;
