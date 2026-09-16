-- Phase 1.1b: validate_run reported "no coverage" whenever coverage had a NULL.
--
-- `select ... into v_coverage` followed by `v_coverage is not null` does not ask
-- "did I find a row". For a record, IS NOT NULL is true only when EVERY field is
-- non-null, and universe.coverage has nullable columns (expected_population,
-- expected_source). A perfectly good run therefore failed validation with
-- market_coverage_present = false.

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
begin
  select * into v_run from pipeline.runs where run_id = p_run_id;
  if not found then
    return jsonb_build_object('ok', false, 'checks', jsonb_build_object('run_exists', false));
  end if;

  select count(*) into v_evaluations from universe.evaluations where run_id = p_run_id;
  select count(*) into v_snapshot from pipeline.master_snapshot where run_id = p_run_id;
  select count(*) into v_fatal from pipeline.run_errors where run_id = p_run_id and severity = 'ERROR';
  select count(*) into v_sources from pipeline.source_fetches where run_id = p_run_id;

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
          = v_coverage.retrieved_count
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
      and (v_checks ->> 'counts_consistent')::boolean,
    'run_id', p_run_id,
    'market_code', v_run.market_code,
    'checks', v_checks
  );
end;
$$;

grant execute on function pipeline.validate_run(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;
