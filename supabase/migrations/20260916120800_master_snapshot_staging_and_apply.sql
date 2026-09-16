-- Phase 1: snapshot staging plus the functions that fan a snapshot out into the
-- security master, universe evaluations and coverage.
--
-- Classification itself is NOT done here: the universe definition lives in the
-- worker code where it is unit tested (RF-12). The snapshot carries the decision
-- that code produced, and these functions only materialise it.

create or replace function ref.deterministic_uuid(p_key text)
returns uuid
language sql
immutable
as $$
  select (substr(m, 1, 8) || '-' || substr(m, 9, 4) || '-' || substr(m, 13, 4) || '-' ||
          substr(m, 17, 4) || '-' || substr(m, 21, 12))::uuid
  from (select md5('surge-research-platform|' || p_key) as m) t;
$$;

comment on function ref.deterministic_uuid(text) is
  'Stable surrogate keys so re-running an ingestion updates the same rows instead of duplicating them.';

create table pipeline.master_snapshot (
  snapshot_row_id      uuid primary key default gen_random_uuid(),
  run_id               uuid not null references pipeline.runs (run_id),
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text not null,
  market_code          ref.market_code not null,
  exchange_id          text references ref.exchanges (exchange_id),
  local_code           text not null,
  symbol               text,
  name                 text not null,
  normalized_name      text not null,
  security_type        ref.security_type not null,
  market_segment_code  text,
  market_segment_name  text,
  currency             text not null,
  country              text not null,
  is_adr               boolean not null default false,
  is_test_issue        boolean not null default false,
  is_spac_pre_merger   boolean,
  listing_status       ref.listing_status not null default 'LISTED',
  cik                  text,
  type_evidence        jsonb not null default '{}'::jsonb,
  decision             universe.decision not null,
  reason_code          text not null references universe.decision_reasons (reason_code),
  decision_detail      jsonb not null default '{}'::jsonb,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_data_version  text not null,
  created_at           timestamptz not null default now(),
  unique (run_id, source_id, source_record_id)
);

comment on table pipeline.master_snapshot is
  'Normalised provider snapshot for one run: exactly what the provider returned plus the classification the worker produced.';

create index master_snapshot_run_idx on pipeline.master_snapshot (run_id);
create index master_snapshot_key_idx on pipeline.master_snapshot (run_id, market_code, exchange_id, local_code);

-- ------------------------------------------------- snapshot -> security master
create or replace function ref.apply_master_snapshot(p_run_id uuid)
returns jsonb
language plpgsql
as $$
declare
  v_issuers int;
  v_securities int;
  v_listings int;
  v_symbols int;
  v_names int;
  v_identifiers int;
  v_status int;
begin
  with src as (
    select country,
           normalized_name,
           min(name) as name,
           min(source_id) as source_id
    from pipeline.master_snapshot
    where run_id = p_run_id
    group by country, normalized_name
  )
  insert into ref.issuers (issuer_id, country, legal_name, normalized_name, name_source)
  select ref.deterministic_uuid('issuer|' || country || '|' || normalized_name),
         country, name, normalized_name, source_id
  from src
  on conflict (issuer_id) do nothing;
  get diagnostics v_issuers = row_count;

  insert into ref.securities (
    security_id, issuer_id, market_code, security_type, security_type_source,
    security_type_evidence, is_adr, is_spac_pre_merger, currency, first_seen_at, last_seen_at
  )
  select ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         ref.deterministic_uuid('issuer|' || s.country || '|' || s.normalized_name),
         s.market_code, s.security_type, s.source_id, s.type_evidence, s.is_adr,
         s.is_spac_pre_merger, s.currency, s.observed_at, s.observed_at
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  on conflict (security_id) do update set
    security_type = excluded.security_type,
    security_type_evidence = excluded.security_type_evidence,
    is_adr = excluded.is_adr,
    is_spac_pre_merger = excluded.is_spac_pre_merger,
    last_seen_at = greatest(ref.securities.last_seen_at, excluded.last_seen_at);
  get diagnostics v_securities = row_count;

  insert into ref.listings (
    listing_id, security_id, exchange_id, local_code, market_segment_code, market_segment_name,
    is_primary, listing_status, effective_from, observed_at, available_at,
    source_id, source_record_id, ingestion_run_id
  )
  select ref.deterministic_uuid('listing|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         s.exchange_id, s.local_code, s.market_segment_code, s.market_segment_name,
         true, s.listing_status, s.observed_at, s.observed_at, s.available_at,
         s.source_id, s.source_record_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.exchange_id is not null
  on conflict (listing_id) do update set
    listing_status = excluded.listing_status,
    market_segment_code = excluded.market_segment_code,
    market_segment_name = excluded.market_segment_name,
    observed_at = excluded.observed_at,
    available_at = excluded.available_at,
    source_record_id = excluded.source_record_id;
  get diagnostics v_listings = row_count;

  -- ticker history: close superseded symbols, then append the current one
  update ref.listing_symbols ls
  set effective_to = s.observed_at
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.exchange_id is not null
    and ls.listing_id = ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.local_code)
    and ls.effective_to is null
    and ls.symbol is distinct from coalesce(s.symbol, s.local_code);

  insert into ref.listing_symbols (
    listing_id, symbol, symbol_type, effective_from, observed_at, available_at,
    source_id, source_record_id, ingestion_run_id
  )
  select ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.local_code),
         coalesce(s.symbol, s.local_code), 'TICKER', s.observed_at, s.observed_at, s.available_at,
         s.source_id, s.source_record_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.exchange_id is not null
  on conflict (listing_id, symbol, symbol_type, effective_from) do nothing;
  get diagnostics v_symbols = row_count;

  insert into ref.security_names (
    security_id, name, normalized_name, name_type, language, effective_from,
    observed_at, available_at, source_id, source_record_id, ingestion_run_id
  )
  select ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         s.name, s.normalized_name, 'LEGAL',
         case when s.market_code = 'JP' then 'ja' else 'en' end,
         s.observed_at, s.observed_at, s.available_at, s.source_id, s.source_record_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  on conflict (security_id, name_type, name, effective_from) do nothing;
  get diagnostics v_names = row_count;

  insert into ref.security_identifiers (
    security_id, id_type, id_value, id_namespace, effective_from,
    observed_at, available_at, source_id, source_record_id, ingestion_run_id
  )
  select ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         case when s.market_code = 'JP' then 'LOCAL_CODE' else 'TICKER' end,
         coalesce(s.symbol, s.local_code),
         s.source_id, s.observed_at, s.observed_at, s.available_at, s.source_id, s.source_record_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  union all
  select ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         'CIK', s.cik, 'sec', s.observed_at, s.observed_at, s.available_at, s.source_id, s.source_record_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.cik is not null
  on conflict (security_id, id_type, id_namespace, id_value, effective_from) do nothing;
  get diagnostics v_identifiers = row_count;

  insert into ref.listing_status_history (
    listing_id, listing_status, effective_from, observed_at, available_at, source_id, ingestion_run_id
  )
  select ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.local_code),
         s.listing_status, s.observed_at, s.observed_at, s.available_at, s.source_id, p_run_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.exchange_id is not null
  on conflict (listing_id, listing_status, effective_from) do nothing;
  get diagnostics v_status = row_count;

  return jsonb_build_object(
    'issuers_inserted', v_issuers,
    'securities_upserted', v_securities,
    'listings_upserted', v_listings,
    'symbols_inserted', v_symbols,
    'names_inserted', v_names,
    'identifiers_inserted', v_identifiers,
    'status_rows_inserted', v_status
  );
end;
$$;

comment on function ref.apply_master_snapshot(uuid) is
  'Materialises one snapshot into the security master. Idempotent: re-running the same snapshot updates rows instead of duplicating them.';

-- --------------------------------------------- snapshot -> universe evaluations
create or replace function universe.apply_snapshot_evaluations(
  p_run_id uuid,
  p_universe_version text,
  p_as_of date
)
returns integer
language plpgsql
as $$
declare
  v_rows int;
begin
  insert into universe.evaluations (
    run_id, universe_version, as_of_date, market_code, security_id, listing_id,
    decision, reason_code, decision_detail, evaluated_at, source_data_version
  )
  select p_run_id, p_universe_version, p_as_of, s.market_code,
         ref.deterministic_uuid('security|' || s.market_code || '|' || coalesce(s.exchange_id, 'NA') || '|' || s.local_code),
         case when s.exchange_id is null then null
              else ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.local_code) end,
         s.decision, s.reason_code, s.decision_detail, now(), s.source_data_version
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  on conflict (run_id, universe_version, as_of_date, listing_id) do nothing;
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

-- ------------------------------------------------------ snapshot -> coverage
create or replace function universe.compute_coverage(
  p_run_id uuid,
  p_universe_version text,
  p_as_of date,
  p_expected jsonb default '{}'::jsonb,
  p_expected_source text default null
)
returns integer
language plpgsql
as $$
declare
  v_rows int;
begin
  with scoped as (
    select 'MARKET'::text as scope_kind, s.market_code::text as scope_value, s.*
    from pipeline.master_snapshot s where s.run_id = p_run_id
    union all
    select 'EXCHANGE', coalesce(s.exchange_id, 'UNRESOLVED'), s.*
    from pipeline.master_snapshot s where s.run_id = p_run_id
    union all
    select 'MARKET_SEGMENT', coalesce(s.market_segment_code, 'UNKNOWN'), s.*
    from pipeline.master_snapshot s where s.run_id = p_run_id
    union all
    select 'SECURITY_TYPE', s.security_type::text, s.*
    from pipeline.master_snapshot s where s.run_id = p_run_id
  ),
  agg as (
    select scope_kind,
           scope_value,
           market_code,
           count(*)::int as retrieved_count,
           count(distinct coalesce(exchange_id, 'NA') || '|' || local_code)::int as unique_count,
           count(*) filter (where security_type <> 'UNKNOWN')::int as classified_count,
           count(*) filter (where decision = 'INCLUDED')::int as included_count,
           count(*) filter (where decision = 'EXCLUDED')::int as excluded_count,
           count(*) filter (where decision = 'UNRESOLVED')::int as unresolved_count
    from scoped
    group by scope_kind, scope_value, market_code
  ),
  errs as (
    select count(*)::int as provider_error_count
    from pipeline.run_errors
    where run_id = p_run_id
  )
  insert into universe.coverage (
    run_id, universe_version, as_of_date, market_code, scope_kind, scope_value,
    expected_population, expected_source, retrieved_count, unique_count, classified_count,
    included_count, excluded_count, unresolved_count, duplicate_count, provider_error_count, computed_at
  )
  select p_run_id, p_universe_version, p_as_of, a.market_code, a.scope_kind, a.scope_value,
         nullif(p_expected ->> (a.scope_kind || ':' || a.scope_value), '')::int,
         p_expected_source,
         a.retrieved_count, a.unique_count, a.classified_count,
         a.included_count, a.excluded_count, a.unresolved_count,
         a.retrieved_count - a.unique_count,
         (select provider_error_count from errs),
         now()
  from agg a
  on conflict (run_id, universe_version, as_of_date, market_code, scope_kind, scope_value) do update set
    expected_population = excluded.expected_population,
    expected_source = excluded.expected_source,
    retrieved_count = excluded.retrieved_count,
    unique_count = excluded.unique_count,
    classified_count = excluded.classified_count,
    included_count = excluded.included_count,
    excluded_count = excluded.excluded_count,
    unresolved_count = excluded.unresolved_count,
    duplicate_count = excluded.duplicate_count,
    provider_error_count = excluded.provider_error_count,
    computed_at = excluded.computed_at;
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function universe.compute_coverage(uuid, text, date, jsonb, text) is
  'Coverage is first class: retrieved / unique / classified / included / excluded / unresolved / duplicates / provider errors per market, exchange, segment and security type.';

grant select, insert, update on pipeline.master_snapshot to surge_worker_prod;
grant select on pipeline.master_snapshot to surge_worker_research, surge_readonly;
grant execute on function ref.deterministic_uuid(text) to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function ref.apply_master_snapshot(uuid) to surge_worker_prod;
grant execute on function universe.apply_snapshot_evaluations(uuid, text, date) to surge_worker_prod;
grant execute on function universe.compute_coverage(uuid, text, date, jsonb, text) to surge_worker_prod;

alter table pipeline.master_snapshot enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'master_snapshot' and policyname = 'surge_worker_prod_all') then
    create policy surge_worker_prod_all on pipeline.master_snapshot as permissive for all to surge_worker_prod using (true) with check (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'master_snapshot' and policyname = 'surge_worker_research_read') then
    create policy surge_worker_research_read on pipeline.master_snapshot as permissive for select to surge_worker_research using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'master_snapshot' and policyname = 'surge_readonly_read') then
    create policy surge_readonly_read on pipeline.master_snapshot as permissive for select to surge_readonly using (true);
  end if;
end
$$;
