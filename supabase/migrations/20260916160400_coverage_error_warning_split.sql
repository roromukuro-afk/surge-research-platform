-- Phase 1.1a: coverage separates provider failures from data quality warnings.
--
-- The US run reported provider_error_count = 246, which reads as "the provider
-- failed 246 times". It was 6 exchange codes the mapper did not know plus 240
-- identity collision warnings over 67 distinct keys - a different fact about a
-- different part of the pipeline.
--
-- The buckets partition pipeline.run_errors for the run (every row lands in
-- exactly one of them), and the collision key count is the number of distinct
-- identities involved, not the number of affected symbols.

alter table universe.coverage
  add column if not exists data_quality_warning_count integer not null default 0,
  add column if not exists identity_collision_record_count integer not null default 0,
  add column if not exists identity_collision_key_count integer not null default 0;

comment on column universe.coverage.provider_error_count is
  'Provider level failures only (fetch, parse, an exchange code that could not be mapped): error_type starting with PROVIDER.';
comment on column universe.coverage.data_quality_warning_count is
  'Other data quality warnings (for example a truncated SEC SIC listing): everything that is neither a provider failure nor an identity collision.';
comment on column universe.coverage.identity_collision_record_count is
  'Records demoted because a reusable identity key was produced more than once.';
comment on column universe.coverage.identity_collision_key_count is
  'Distinct identity keys involved in those collisions. 240 records over 67 keys is not 240 provider errors.';

create or replace function universe.compute_coverage(
  p_run_id uuid,
  p_universe_version text,
  p_as_of date,
  p_expected jsonb default '{}'::jsonb,
  p_expected_source text default null
)
returns integer
language plpgsql
set search_path = ''
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
    select
      count(*) filter (where coalesce(error_type, '') like 'PROVIDER%')::int as provider_error_count,
      count(*) filter (
        where coalesce(error_type, '') not like 'PROVIDER%'
          and coalesce(error_type, '') <> 'IDENTITY_COLLISION'
      )::int as data_quality_warning_count,
      count(*) filter (where error_type = 'IDENTITY_COLLISION')::int as identity_collision_record_count,
      count(distinct context ->> 'identity_key')
        filter (where error_type = 'IDENTITY_COLLISION')::int as identity_collision_key_count
    from pipeline.run_errors
    where run_id = p_run_id
  )
  insert into universe.coverage (
    run_id, universe_version, as_of_date, market_code, scope_kind, scope_value,
    expected_population, expected_source, retrieved_count, unique_count, classified_count,
    included_count, excluded_count, unresolved_count, duplicate_count, provider_error_count,
    data_quality_warning_count, identity_collision_record_count, identity_collision_key_count,
    computed_at
  )
  select p_run_id, p_universe_version, p_as_of, a.market_code, a.scope_kind, a.scope_value,
         nullif(p_expected ->> (a.scope_kind || ':' || a.scope_value), '')::int,
         p_expected_source,
         a.retrieved_count, a.unique_count, a.classified_count,
         a.included_count, a.excluded_count, a.unresolved_count,
         a.retrieved_count - a.unique_count,
         (select provider_error_count from errs),
         (select data_quality_warning_count from errs),
         (select identity_collision_record_count from errs),
         (select identity_collision_key_count from errs),
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
    data_quality_warning_count = excluded.data_quality_warning_count,
    identity_collision_record_count = excluded.identity_collision_record_count,
    identity_collision_key_count = excluded.identity_collision_key_count,
    computed_at = excluded.computed_at;
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function universe.compute_coverage(uuid, text, date, jsonb, text) is
  'Coverage is first class: retrieved / unique / classified / included / excluded / unresolved / duplicates, plus run diagnostics split into provider errors, data quality warnings and identity collisions (records and distinct keys).';

grant execute on function universe.compute_coverage(uuid, text, date, jsonb, text) to surge_worker_prod;
