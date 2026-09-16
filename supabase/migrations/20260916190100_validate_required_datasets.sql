-- Phase 2.0 carry-forward: validation requires DATASETS, not providers.
--
-- required_sources now names SEC_SIC_6770 and SEC_SIC_6798 separately, so the
-- check has to match on the fetch's dataset_key. Before this, one SIC fetch
-- with a digest satisfied "sec_sic_directory" and the other could be missing
-- entirely while the run published cleanly.
--
-- pipeline.fetch_dataset_key falls back to the endpoint, so runs made before the
-- dataset_key column - which are published and therefore immutable - are judged
-- by the same rule without being rewritten.

create or replace function pipeline.validate_run(p_run_id uuid)
returns jsonb
language plpgsql
stable
set search_path = ''
as $$
declare
  v_run pipeline.runs%rowtype;
  v_checks jsonb;
  v_coverage universe.coverage%rowtype;
  v_has_coverage boolean := false;
  v_evaluations int;
  v_snapshot int;
  v_fatal int;
  v_sources int;
  v_foreign_market int;
  v_snapshot_versions int;
  v_snapshot_data_version text;
  v_snapshot_identity_versions int;
  v_snapshot_identity_version text;
  v_evaluation_versions int;
  v_evaluation_version text;
  v_missing_available int;
  v_max_available timestamptz;
  v_missing_required text[];
  v_incomplete_sources int;
begin
  select * into v_run from pipeline.runs where run_id = p_run_id;
  if not found then
    return jsonb_build_object('ok', false, 'checks', jsonb_build_object('run_exists', false));
  end if;

  select count(*) into v_evaluations from universe.evaluations where run_id = p_run_id;
  select count(*) into v_snapshot from pipeline.master_snapshot where run_id = p_run_id;
  select count(*) into v_fatal from pipeline.run_errors where run_id = p_run_id and severity = 'ERROR';
  select count(*) into v_sources from pipeline.source_fetches where run_id = p_run_id;

  select count(*) into v_incomplete_sources
  from pipeline.run_errors
  where run_id = p_run_id and error_type = 'CRITICAL_SOURCE_INCOMPLETE';

  select count(*) into v_foreign_market
  from pipeline.master_snapshot
  where run_id = p_run_id and market_code <> v_run.market_code;

  select count(distinct source_data_version), min(source_data_version)
    into v_snapshot_versions, v_snapshot_data_version
  from pipeline.master_snapshot where run_id = p_run_id;

  select count(distinct coalesce(identity_version, '')), min(coalesce(identity_version, ''))
    into v_snapshot_identity_versions, v_snapshot_identity_version
  from pipeline.master_snapshot where run_id = p_run_id;

  select count(distinct universe_version), min(universe_version)
    into v_evaluation_versions, v_evaluation_version
  from universe.evaluations where run_id = p_run_id;

  select count(*) filter (where available_at is null), max(available_at)
    into v_missing_available, v_max_available
  from pipeline.source_fetches where run_id = p_run_id;

  -- by DATASET, not by provider: one source_id can serve several datasets that
  -- are each required (SEC SIC 6770 and 6798).
  select coalesce(array_agg(required), '{}')
    into v_missing_required
  from unnest(pipeline.required_sources(v_run.market_code)) as required
  where not exists (
    select 1 from pipeline.source_fetches f
    where f.run_id = p_run_id
      and pipeline.fetch_dataset_key(f.source_id, f.endpoint, f.dataset_key) = required
      and f.content_sha256 ~ '^[0-9a-f]{64}$'
  );

  select * into v_coverage
  from universe.coverage
  where run_id = p_run_id and scope_kind = 'MARKET' and market_code = v_run.market_code;
  v_has_coverage := found;

  v_checks := jsonb_build_object(
    'status_succeeded', v_run.status = 'SUCCEEDED',
    'finished_at_present', v_run.finished_at is not null,
    'git_sha_present', v_run.git_sha is not null,
    'config_hash_present', v_run.config_hash is not null,
    'job_version_present', v_run.job_version is not null,
    'universe_version_present', (v_run.versions ? 'universe_version'),
    'identity_version_present', (v_run.versions ? 'identity_version'),
    'provider_bindings_present', v_run.provider_bindings is not null and v_run.provider_bindings <> '{}'::jsonb,
    'source_fetches_present', v_sources > 0,
    'market_coverage_present', v_has_coverage,
    'no_fatal_errors', v_fatal = 0,
    'snapshot_rows', v_snapshot,
    'evaluation_rows', v_evaluations,
    'coverage_retrieved', case when v_has_coverage then v_coverage.retrieved_count else -1 end,
    'coverage_unique', case when v_has_coverage then v_coverage.unique_count else -1 end,
    'counts_consistent',
      v_has_coverage
      and v_coverage.retrieved_count = v_snapshot
      and v_evaluations = v_coverage.unique_count
      and (v_coverage.included_count + v_coverage.excluded_count + v_coverage.unresolved_count)
          = v_coverage.retrieved_count,
    -- Phase 1.1c: the content has to agree with what the run says about itself
    'snapshot_market_consistent', v_foreign_market = 0,
    'snapshot_data_version_single', v_snapshot_versions = 1,
    'snapshot_data_version_matches_run',
      v_snapshot_versions = 1 and v_snapshot_data_version = (v_run.params ->> 'source_data_version'),
    'snapshot_identity_version_matches_run',
      v_snapshot_identity_versions = 1
      and v_snapshot_identity_version = (v_run.versions ->> 'identity_version'),
    'evaluation_universe_version_matches_run',
      v_evaluations = 0
      or (v_evaluation_versions = 1 and v_evaluation_version = (v_run.versions ->> 'universe_version')),
    'source_available_at_complete', v_missing_available = 0,
    'required_sources_hashed', coalesce(array_length(v_missing_required, 1), 0) = 0,
    'missing_required_sources', to_jsonb(v_missing_required),
    'data_cutoff_covers_sources',
      v_run.data_cutoff is not null and v_max_available is not null
      and v_run.data_cutoff >= v_max_available,
    'critical_sources_complete', v_incomplete_sources = 0
  );

  return jsonb_build_object(
    'ok',
    (v_checks ->> 'status_succeeded')::boolean
      and (v_checks ->> 'finished_at_present')::boolean
      and (v_checks ->> 'git_sha_present')::boolean
      and (v_checks ->> 'config_hash_present')::boolean
      and (v_checks ->> 'job_version_present')::boolean
      and (v_checks ->> 'universe_version_present')::boolean
      and (v_checks ->> 'identity_version_present')::boolean
      and (v_checks ->> 'provider_bindings_present')::boolean
      and (v_checks ->> 'source_fetches_present')::boolean
      and (v_checks ->> 'market_coverage_present')::boolean
      and (v_checks ->> 'no_fatal_errors')::boolean
      and (v_checks ->> 'counts_consistent')::boolean
      and (v_checks ->> 'snapshot_market_consistent')::boolean
      and (v_checks ->> 'snapshot_data_version_single')::boolean
      and (v_checks ->> 'snapshot_data_version_matches_run')::boolean
      and (v_checks ->> 'snapshot_identity_version_matches_run')::boolean
      and (v_checks ->> 'evaluation_universe_version_matches_run')::boolean
      and (v_checks ->> 'source_available_at_complete')::boolean
      and (v_checks ->> 'required_sources_hashed')::boolean
      and (v_checks ->> 'data_cutoff_covers_sources')::boolean
      and (v_checks ->> 'critical_sources_complete')::boolean,
    'run_id', p_run_id,
    'market_code', v_run.market_code,
    'checks', v_checks
  );
end;
$$;

comment on function pipeline.validate_run(uuid) is
  'What a run must satisfy before publication: finished, attributable, complete, internally consistent, every required DATASET fetched with a real digest, data_cutoff covering all of them, and no critical classification source truncated.';

grant execute on function pipeline.validate_run(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;
