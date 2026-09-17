-- Phase 2.1: prices, corporate actions, FX and identifier mappings.
--
-- The organising rule is that raw and adjusted never share a row, and no column
-- is called raw unless the provider says it is raw. EODHD's OHLC are as traded
-- but its volume is split adjusted; J-Quants supplies both series side by side
-- and recomputes its adjusted one retroactively without limit. A schema that
-- stored "open, high, low, close, volume" would quietly average those two very
-- different things, so every price column carries its own declared basis.
--
-- Bitemporality follows Phase 1: observed_at is when we read the source,
-- available_at is when the system could have known it, and availability_basis
-- says which of those is a measurement and which is an assumption. A ten-year
-- old bar fetched today is OBSERVED_NOW - it must not pretend the system knew
-- it ten years ago.

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'price_basis' and n.nspname = 'market') then
    create type market.price_basis as enum (
      'RAW',                          -- as traded; no adjustment applied
      'SPLIT_ADJUSTED',
      'SPLIT_AND_DIVIDEND_ADJUSTED',
      'PROVIDER_UNSPECIFIED'          -- the provider does not document it. Not a synonym for RAW.
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'corporate_action_type' and n.nspname = 'market') then
    create type market.corporate_action_type as enum (
      'SPLIT', 'REVERSE_SPLIT', 'RIGHTS_ISSUE', 'STOCK_DIVIDEND',
      'CASH_DIVIDEND', 'SPINOFF', 'ADR_RATIO_CHANGE', 'OTHER', 'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'coverage_quality' and n.nspname = 'market') then
    create type market.coverage_quality as enum (
      'COMPLETE', 'PARTIAL_KNOWN_GAP', 'NOT_PROVIDED', 'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'fx_rate_kind' and n.nspname = 'market') then
    create type market.fx_rate_kind as enum (
      'REFERENCE_RATE',   -- published for information, not for transactions (ECB)
      'MARKET_RATE',
      'INDICATIVE'        -- the provider disclaims it as indicative (EODHD forex)
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'figi_mapping_status' and n.nspname = 'market') then
    create type market.figi_mapping_status as enum ('EXACT', 'AMBIGUOUS', 'UNMAPPED', 'ERROR');
  end if;
end;
$$;

-- ------------------------------------------------------------------- bars
create table if not exists market.daily_bars (
  provider_id text not null,
  dataset_key text not null,
  market_code ref.market_code not null,
  native_symbol text not null,
  trade_date date not null,

  exchange_code text,
  currency text not null,
  security_id uuid references ref.securities (security_id),

  open numeric(20, 6),
  high numeric(20, 6),
  low numeric(20, 6),
  close numeric(20, 6),
  volume numeric(28, 6),
  turnover numeric(28, 4),

  open_basis market.price_basis not null,
  high_basis market.price_basis not null,
  low_basis market.price_basis not null,
  close_basis market.price_basis not null,
  volume_basis market.price_basis not null,

  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  source_published_at timestamptz,
  ingested_at timestamptz not null default clock_timestamp(),

  raw_object_key text not null references market.raw_objects (object_key),
  run_id uuid references pipeline.runs (run_id),
  revision integer not null default 1,

  primary key (provider_id, dataset_key, native_symbol, trade_date),
  constraint daily_bars_ohlc_ck check (
    (high is null or low is null or high >= low)
    and (open is null or high is null or open <= high)
    and (close is null or high is null or close <= high)
    and (open is null or low is null or open >= low)
    and (close is null or low is null or close >= low)
  ),
  constraint daily_bars_volume_ck check (volume is null or volume >= 0)
);

comment on table market.daily_bars is
  'One vendor-native daily bar. Each price column declares its own adjustment basis, because providers mix them: EODHD returns as-traded OHLC alongside a split-adjusted volume.';
comment on column market.daily_bars.volume_basis is
  'What the volume actually is. EODHD''s is SPLIT_ADJUSTED. Never set this to RAW to make a query simpler - a reconstructed raw volume belongs in market.derived_volumes.';
comment on column market.daily_bars.security_id is
  'Resolved against the Phase 1 security master where possible. Null is normal and is not an error: ingestion must not be blocked by identity resolution, and an unresolved bar is still evidence.';
comment on column market.daily_bars.availability_basis is
  'How available_at was arrived at. A historical backfill is OBSERVED_NOW: we learned it today.';

create index if not exists daily_bars_security_idx on market.daily_bars (security_id, trade_date);
create index if not exists daily_bars_date_idx on market.daily_bars (market_code, trade_date);
create index if not exists daily_bars_object_idx on market.daily_bars (raw_object_key);

-- Providers correct silently. Keep what the row used to say.
create table if not exists market.daily_bar_revisions (
  revision_id bigint generated always as identity primary key,
  provider_id text not null,
  dataset_key text not null,
  native_symbol text not null,
  trade_date date not null,
  revision integer not null,
  previous_values jsonb not null,
  new_values jsonb not null,
  previous_raw_object_key text,
  new_raw_object_key text,
  detected_at timestamptz not null default clock_timestamp(),
  detected_by_run_id uuid references pipeline.runs (run_id)
);

comment on table market.daily_bar_revisions is
  'Every time a provider''s answer for a bar changed under us. J-Quants states that corrections overwrite the existing data with no version, ETag or diff, so this is the only place a correction is visible.';

create index if not exists daily_bar_revisions_bar_idx
  on market.daily_bar_revisions (provider_id, dataset_key, native_symbol, trade_date);

create or replace function market.record_daily_bar_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_old jsonb := to_jsonb(old) - 'ingested_at' - 'revision' - 'raw_object_key' - 'run_id'
                 - 'observed_at' - 'available_at' - 'source_published_at';
  v_new jsonb := to_jsonb(new) - 'ingested_at' - 'revision' - 'raw_object_key' - 'run_id'
                 - 'observed_at' - 'available_at' - 'source_published_at';
begin
  if v_old is not distinct from v_new then
    return new;
  end if;

  insert into market.daily_bar_revisions (
    provider_id, dataset_key, native_symbol, trade_date, revision,
    previous_values, new_values, previous_raw_object_key, new_raw_object_key, detected_by_run_id
  ) values (
    old.provider_id, old.dataset_key, old.native_symbol, old.trade_date, old.revision,
    v_old, v_new, old.raw_object_key, new.raw_object_key, new.run_id
  );

  new.revision := old.revision + 1;
  return new;
end;
$$;

drop trigger if exists record_daily_bar_revision on market.daily_bars;
create trigger record_daily_bar_revision
  before update on market.daily_bars
  for each row execute function market.record_daily_bar_revision();

-- ---------------------------------------------------------- adjusted series
create table if not exists market.daily_bars_adjusted (
  provider_id text not null,
  dataset_key text not null,
  native_symbol text not null,
  trade_date date not null,

  adj_open numeric(20, 6),
  adj_high numeric(20, 6),
  adj_low numeric(20, 6),
  adj_close numeric(20, 6),
  adj_volume numeric(28, 6),
  adjusted_close numeric(20, 6),

  adjustment_basis market.price_basis not null,
  adjusted_close_basis market.price_basis,
  adjustment_factor numeric(20, 10),
  ex_event_code text,

  recomputed_retroactively boolean not null default false,
  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  ingested_at timestamptz not null default clock_timestamp(),
  raw_object_key text not null references market.raw_objects (object_key),
  run_id uuid references pipeline.runs (run_id),

  primary key (provider_id, dataset_key, native_symbol, trade_date),
  constraint daily_bars_adjusted_bar_fk
    foreign key (provider_id, dataset_key, native_symbol, trade_date)
    references market.daily_bars (provider_id, dataset_key, native_symbol, trade_date)
);

comment on table market.daily_bars_adjusted is
  'The provider''s own adjusted series, kept in its own table so no query can reach a raw and an adjusted number in one row by accident.';
comment on column market.daily_bars_adjusted.recomputed_retroactively is
  'True when the provider recomputes this series back through history on every new split - J-Quants says it does, with no limit on how far back. Such a value is not stable across fetches and must not be treated as a historical fact.';
comment on column market.daily_bars_adjusted.ex_event_code is
  'The provider''s own ex-event marker as supplied (J-Quants ExRT: 1 split, 2 reverse split, 3 rights issue). Kept verbatim; the interpreted event goes to market.corporate_actions.';

-- ------------------------------------------------------- corporate actions
create table if not exists market.corporate_actions (
  corporate_action_id bigint generated always as identity primary key,
  provider_id text not null,
  dataset_key text not null,
  market_code ref.market_code not null,
  native_symbol text not null,
  security_id uuid references ref.securities (security_id),

  action_type market.corporate_action_type not null,
  ex_date date not null,
  record_date date,
  payment_date date,
  declaration_date date,

  split_from numeric(20, 10),
  split_to numeric(20, 10),
  adjustment_factor numeric(20, 10),
  amount numeric(20, 10),
  unadjusted_amount numeric(20, 10),
  currency text,

  provider_native_type text,
  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  ingested_at timestamptz not null default clock_timestamp(),
  raw_object_key text not null references market.raw_objects (object_key),
  run_id uuid references pipeline.runs (run_id),

  unique (provider_id, dataset_key, native_symbol, ex_date, action_type, provider_native_type)
);

comment on table market.corporate_actions is
  'Splits, dividends and the rest, kept apart from prices. A raw price series plus these is reconstructible; an adjusted series alone is not.';
comment on column market.corporate_actions.unadjusted_amount is
  'The amount actually paid per share on the day, in the share count of the time. EODHD supplies both; never compare an adjusted amount with an unadjusted price.';

create index if not exists corporate_actions_symbol_idx
  on market.corporate_actions (provider_id, native_symbol, ex_date);
create index if not exists corporate_actions_security_idx
  on market.corporate_actions (security_id, ex_date);

-- What we know about how complete that is, per security.
create table if not exists market.security_coverage (
  provider_id text not null,
  dataset_key text not null,
  native_symbol text not null,
  market_code ref.market_code not null,

  first_trade_date date,
  last_trade_date date,
  is_delisted boolean,
  delisted_on date,

  corporate_action_completeness market.coverage_quality not null default 'UNKNOWN',
  completeness_reason text,

  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  ingested_at timestamptz not null default clock_timestamp(),
  raw_object_key text references market.raw_objects (object_key),
  run_id uuid references pipeline.runs (run_id),

  primary key (provider_id, dataset_key, native_symbol)
);

comment on table market.security_coverage is
  'Per security, how far the provider''s data reaches and how complete its corporate actions are. EODHD documents that securities delisted before 2018 have end-of-day prices but no splits or dividends, which makes a raw/adjusted reconciliation impossible for those names - that is recorded as PARTIAL_KNOWN_GAP, not discovered later by a puzzled analyst.';

-- ------------------------------------------------- reconstructed raw volume
create table if not exists market.derived_volumes (
  provider_id text not null,
  dataset_key text not null,
  native_symbol text not null,
  trade_date date not null,

  derived_volume numeric(28, 6) not null,
  reconstruction_method text not null,
  split_source_dataset text not null,
  cumulative_factor numeric(20, 10),
  coverage_quality market.coverage_quality not null,
  notes text,

  computed_at timestamptz not null default clock_timestamp(),
  run_id uuid references pipeline.runs (run_id),

  primary key (provider_id, dataset_key, native_symbol, trade_date),
  constraint derived_volumes_bar_fk
    foreign key (provider_id, dataset_key, native_symbol, trade_date)
    references market.daily_bars (provider_id, dataset_key, native_symbol, trade_date)
);

comment on table market.derived_volumes is
  'Raw share counts reconstructed from a split-adjusted volume. Kept separate and never written back over the vendor''s own number: this is our inference, and it is only as good as the split history it was built from.';

-- ------------------------------------------------------------------- FX
create table if not exists market.fx_rates (
  provider_id text not null,
  dataset_key text not null,
  source_date date not null,

  eur_usd numeric(20, 10),
  eur_jpy numeric(20, 10),
  derived_usd_jpy numeric(20, 10),
  derivation_method text not null,
  eur_usd_decimals integer,
  eur_jpy_decimals integer,

  rate_kind market.fx_rate_kind not null,
  source_published_at timestamptz,
  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  ingested_at timestamptz not null default clock_timestamp(),

  content_sha256 text not null,
  raw_object_key text not null references market.raw_objects (object_key),
  run_id uuid references pipeline.runs (run_id),
  notes text,
  revision integer not null default 1,

  primary key (provider_id, dataset_key, source_date),
  constraint fx_rates_sha_ck check (content_sha256 ~ '^[0-9a-f]{64}$'),
  constraint fx_rates_positive_ck check (
    (eur_usd is null or eur_usd > 0)
    and (eur_jpy is null or eur_jpy > 0)
    and (derived_usd_jpy is null or derived_usd_jpy > 0)
  )
);

comment on table market.fx_rates is
  'USD/JPY, stored as the two euro legs the ECB actually publishes plus the cross we derive from them. Both legs are kept because the quotient rounds: the ECB publishes USD/EUR to 4 decimals and JPY/EUR to 2.';
comment on column market.fx_rates.derivation_method is
  'How derived_usd_jpy was computed, in full, so a later reader does not have to guess which leg was the numerator.';
comment on column market.fx_rates.rate_kind is
  'REFERENCE_RATE for the ECB: it publishes these for information and discourages their use for transactions. That is a property of the number and travels with it.';
comment on column market.fx_rates.source_published_at is
  'When the provider says it published this, when the provider says so at all (the ECB data API returns Last-Modified). Distinct from observed_at, which is when we read it.';

create table if not exists market.fx_rate_revisions (
  revision_id bigint generated always as identity primary key,
  provider_id text not null,
  dataset_key text not null,
  source_date date not null,
  revision integer not null,
  previous_values jsonb not null,
  new_values jsonb not null,
  previous_sha256 text,
  new_sha256 text,
  detected_at timestamptz not null default clock_timestamp(),
  detected_by_run_id uuid references pipeline.runs (run_id)
);

comment on table market.fx_rate_revisions is
  'Changes to an FX observation we had already stored. The ECB states it will not amend a rate after the next business day''s rate is published, but a stated policy is not a guarantee, so we check rather than assume.';

create or replace function market.record_fx_rate_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_old jsonb := jsonb_build_object('eur_usd', old.eur_usd, 'eur_jpy', old.eur_jpy,
                                    'derived_usd_jpy', old.derived_usd_jpy,
                                    'source_published_at', old.source_published_at);
  v_new jsonb := jsonb_build_object('eur_usd', new.eur_usd, 'eur_jpy', new.eur_jpy,
                                    'derived_usd_jpy', new.derived_usd_jpy,
                                    'source_published_at', new.source_published_at);
begin
  if v_old is not distinct from v_new and old.content_sha256 is not distinct from new.content_sha256 then
    return new;
  end if;

  insert into market.fx_rate_revisions (
    provider_id, dataset_key, source_date, revision,
    previous_values, new_values, previous_sha256, new_sha256, detected_by_run_id
  ) values (
    old.provider_id, old.dataset_key, old.source_date, old.revision,
    v_old, v_new, old.content_sha256, new.content_sha256, new.run_id
  );

  new.revision := old.revision + 1;
  return new;
end;
$$;

drop trigger if exists record_fx_rate_revision on market.fx_rates;
create trigger record_fx_rate_revision
  before update on market.fx_rates
  for each row execute function market.record_fx_rate_revision();

-- The eligibility reader. It answers with what was knowable at the cutoff, and
-- says how old that was rather than pretending it is same-day.
create or replace function market.usdjpy_as_of(
  p_knowledge_cutoff timestamptz,
  p_provider_id text default 'ecb',
  p_dataset_key text default 'ECB_EXR_DAILY'
)
returns table (
  source_date date,
  rate numeric,
  eur_usd numeric,
  eur_jpy numeric,
  derivation_method text,
  rate_kind market.fx_rate_kind,
  observed_at timestamptz,
  available_at timestamptz,
  availability_basis market.availability_basis,
  fx_age_seconds numeric
)
language sql
stable
set search_path = ''
as $$
  select f.source_date,
         f.derived_usd_jpy,
         f.eur_usd,
         f.eur_jpy,
         f.derivation_method,
         f.rate_kind,
         f.observed_at,
         f.available_at,
         f.availability_basis,
         extract(epoch from (p_knowledge_cutoff - f.available_at))::numeric
  from market.fx_rates f
  where f.provider_id = p_provider_id
    and f.dataset_key = p_dataset_key
    and f.available_at <= p_knowledge_cutoff
    and f.derived_usd_jpy is not null
  order by f.available_at desc, f.source_date desc
  limit 1;
$$;

comment on function market.usdjpy_as_of(timestamptz, text, text) is
  'The most recent USD/JPY the system could have known at this cutoff, with its own source_date and its age in seconds. The caller stores both: a rate carried forward across a TARGET closing day is not a same-day rate and must not be recorded as one.';

-- ------------------------------------------------------- OpenFIGI mappings
create table if not exists market.figi_mappings (
  mapping_id bigint generated always as identity primary key,
  run_id uuid references pipeline.runs (run_id),

  input_id_type text not null,
  input_id_value text not null,
  input_exch_code text,
  input_mic_code text,
  input_security_type2 text,
  include_unlisted_equities boolean not null default false,

  mapping_status market.figi_mapping_status not null,
  match_count integer not null,
  match_index integer,

  figi text,
  composite_figi text,
  share_class_figi text,
  ticker text,
  exch_code text,
  security_type text,
  security_type2 text,
  name text,
  security_description text,
  market_sector text,

  warning text,
  error text,
  requested_at timestamptz not null,
  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',
  raw_object_key text references market.raw_objects (object_key),

  constraint figi_mappings_match_ck check (
    (mapping_status in ('UNMAPPED', 'ERROR') and match_index is null)
    or (mapping_status in ('EXACT', 'AMBIGUOUS') and match_index is not null)
  )
);

comment on table market.figi_mappings is
  'Research enrichment only. Every match OpenFIGI returned is stored, including all of them when a ticker maps to more than one issuer; nothing here changes a security_id. Promotion of US identity to a share-class FIGI is a separate, recorded identity migration.';
comment on column market.figi_mappings.mapping_status is
  'EXACT when exactly one match came back, AMBIGUOUS when several did (FRCB returns two different issuers), UNMAPPED on OpenFIGI''s "No identifier found." warning, ERROR on a job-level error.';
comment on column market.figi_mappings.match_index is
  'Position within the matches for one input, so an ambiguous result keeps all its candidates rather than silently collapsing to the first.';

create unique index if not exists figi_mappings_input_uq
  on market.figi_mappings (run_id, input_id_type, input_id_value,
                           coalesce(input_exch_code, ''), coalesce(match_index, -1));
create index if not exists figi_mappings_share_class_idx
  on market.figi_mappings (share_class_figi) where share_class_figi is not null;
create index if not exists figi_mappings_status_idx on market.figi_mappings (mapping_status);

create or replace view market.figi_mapping_coverage as
  select run_id,
         count(distinct (input_id_type, input_id_value, input_exch_code)) as inputs,
         count(distinct (input_id_type, input_id_value, input_exch_code))
           filter (where mapping_status = 'EXACT') as exact,
         count(distinct (input_id_type, input_id_value, input_exch_code))
           filter (where mapping_status = 'AMBIGUOUS') as ambiguous,
         count(distinct (input_id_type, input_id_value, input_exch_code))
           filter (where mapping_status = 'UNMAPPED') as unmapped,
         count(distinct (input_id_type, input_id_value, input_exch_code))
           filter (where mapping_status = 'ERROR') as errored
  from market.figi_mappings
  group by run_id;

comment on view market.figi_mapping_coverage is
  'What a mapping run actually resolved. The coverage report Phase 2 owes before any identity promotion is argued for.';

-- ---------------------------------------------- source level revision log
create table if not exists market.source_revisions (
  source_revision_id bigint generated always as identity primary key,
  provider_id text not null,
  dataset_key text not null,
  natural_key text not null,
  previous_sha256 text,
  new_sha256 text not null,
  previous_object_key text references market.raw_objects (object_key),
  new_object_key text not null references market.raw_objects (object_key),
  detected_at timestamptz not null default clock_timestamp(),
  detected_by_run_id uuid references pipeline.runs (run_id),
  notes text
);

comment on table market.source_revisions is
  'A rolling refetch found different bytes behind the same request. This is how a provider that corrects data by silent overwrite becomes visible.';

create index if not exists source_revisions_dataset_idx
  on market.source_revisions (provider_id, dataset_key, natural_key, detected_at desc);

-- -------------------------------------------------- purging derived rows
create or replace function market.purge_market_rows(p_purge_request_id bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dry_run boolean;
  v_bars integer;
  v_adjusted integer;
  v_actions integer;
  v_coverage integer;
  v_fx integer;
  v_derived integer;
begin
  select dry_run into v_dry_run from market.purge_requests where purge_request_id = p_purge_request_id;
  if not found then
    raise exception 'no such purge request %', p_purge_request_id;
  end if;

  if v_dry_run then
    select count(*) into v_bars from market.daily_bars b
      join market.purge_targets t on t.object_key = b.raw_object_key
     where t.purge_request_id = p_purge_request_id;
    return jsonb_build_object('dry_run', true, 'daily_bars', v_bars);
  end if;

  -- Derived rows first, then the rows they reference.
  delete from market.derived_volumes d
  using market.daily_bars b, market.purge_targets t
  where d.provider_id = b.provider_id and d.dataset_key = b.dataset_key
    and d.native_symbol = b.native_symbol and d.trade_date = b.trade_date
    and t.object_key = b.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_derived = row_count;

  delete from market.daily_bars_adjusted a
  using market.purge_targets t
  where t.object_key = a.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_adjusted = row_count;

  delete from market.daily_bars b
  using market.purge_targets t
  where t.object_key = b.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_bars = row_count;

  delete from market.corporate_actions c
  using market.purge_targets t
  where t.object_key = c.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_actions = row_count;

  delete from market.security_coverage s
  using market.purge_targets t
  where t.object_key = s.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_coverage = row_count;

  delete from market.fx_rates f
  using market.purge_targets t
  where t.object_key = f.raw_object_key and t.purge_request_id = p_purge_request_id;
  get diagnostics v_fx = row_count;

  return jsonb_build_object(
    'dry_run', false,
    'derived_volumes', v_derived,
    'daily_bars_adjusted', v_adjusted,
    'daily_bars', v_bars,
    'corporate_actions', v_actions,
    'security_coverage', v_coverage,
    'fx_rates', v_fx
  );
end;
$$;

comment on function market.purge_market_rows(bigint) is
  'Deletes the Postgres rows derived from the objects a purge covers. Runs as the purge principal, never as the ingestion worker, and refuses to delete anything on a dry run.';

-- ----------------------------------------------------------------- privileges
revoke all on table
  market.daily_bars, market.daily_bars_adjusted, market.daily_bar_revisions,
  market.corporate_actions, market.security_coverage, market.derived_volumes,
  market.fx_rates, market.fx_rate_revisions, market.figi_mappings, market.source_revisions
from public;

grant select on table
  market.daily_bars, market.daily_bars_adjusted, market.daily_bar_revisions,
  market.corporate_actions, market.security_coverage, market.derived_volumes,
  market.fx_rates, market.fx_rate_revisions, market.figi_mappings, market.source_revisions
to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

-- The ingestion worker writes market data. It still has no DELETE anywhere.
grant insert, update on table
  market.daily_bars, market.daily_bars_adjusted, market.corporate_actions,
  market.security_coverage, market.derived_volumes, market.fx_rates
to surge_worker_prod;
grant insert on table
  market.daily_bar_revisions, market.fx_rate_revisions, market.figi_mappings, market.source_revisions
to surge_worker_prod;

grant delete on table
  market.daily_bars, market.daily_bars_adjusted, market.corporate_actions,
  market.security_coverage, market.derived_volumes, market.fx_rates
to surge_purge;

grant select on market.figi_mapping_coverage
  to surge_worker_prod, surge_worker_research, surge_readonly;

grant execute on function market.usdjpy_as_of(timestamptz, text, text)
  to surge_worker_prod, surge_worker_research, surge_readonly;

revoke all on function market.purge_market_rows(bigint) from public;
grant execute on function market.purge_market_rows(bigint) to surge_purge;
