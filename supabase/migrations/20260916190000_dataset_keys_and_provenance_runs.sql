-- Phase 2.0 carry-forward: four schema corrections from the Phase 1.1c audit.
--
-- A. One provider, several required datasets. The SEC SIC directory supplies
--    blank-check (6770) and REIT (6798) membership under one source_id, so
--    "sec_sic_directory was fetched" did not prove both were. A fetch now names
--    its dataset and validation requires each dataset separately.
-- B. provider_bindings keyed by source_id could only hold one of the two SIC
--    endpoints (worker side; the column is free-form jsonb).
-- C. The validation ruleset changed in Phase 1.1c while publications still
--    recorded publication-1.0.0. The version is now derived from the ruleset.
-- D. ingestion_run_id was doing two jobs: "which run created this row" and
--    "which run last confirmed where it came from". They are separated.
-- E. (documentation, see docs/specs/security-identity.md)

-- ------------------------------------------------------------ A. dataset key
alter table pipeline.source_fetches add column if not exists dataset_key text;

comment on column pipeline.source_fetches.dataset_key is
  'Which dataset this fetch supplied. Usually the source_id, but a provider that serves several independently required datasets (SEC SIC 6770 vs 6798) names each one. Validation requires datasets, not providers.';

-- Runs published before this column existed are immutable, so their rows are
-- NOT backfilled - an UPDATE would be exactly the artifact rewrite Phase 1.1c
-- forbids. The dataset was always derivable from the endpoint; this function
-- says so, and the column only records it explicitly from now on.
create or replace function pipeline.fetch_dataset_key(
  p_source_id text,
  p_endpoint text,
  p_dataset_key text default null
)
returns text
language sql
immutable
set search_path = ''
as $$
  select coalesce(
    p_dataset_key,
    case
      when p_source_id = 'sec_sic_directory' and p_endpoint like '%SIC=6770%' then 'SEC_SIC_6770'
      when p_source_id = 'sec_sic_directory' and p_endpoint like '%SIC=6798%' then 'SEC_SIC_6798'
      else p_source_id
    end
  );
$$;

comment on function pipeline.fetch_dataset_key(text, text, text) is
  'The dataset a fetch supplied: the recorded dataset_key when there is one, otherwise derived from source_id and endpoint so that runs predating the column are read correctly without being rewritten.';

grant execute on function pipeline.fetch_dataset_key(text, text, text)
  to surge_worker_prod, surge_worker_research, surge_readonly;

create or replace function pipeline.required_sources(p_market_code ref.market_code)
returns text[]
language sql
immutable
set search_path = ''
as $$
  select case p_market_code
           when 'JP' then array['jpx_listed_issues', 'edinet_code_list']
           when 'US' then array[
             'nasdaq_trader_symbol_directory',
             'sec_company_tickers',
             'SEC_SIC_6770',   -- blank check: decides SPAC_PRE_MERGER
             'SEC_SIC_6798'    -- REIT: decides REIT_OR_FUND
           ]
           else array[]::text[]
         end;
$$;

comment on function pipeline.required_sources(ref.market_code) is
  'Datasets a run of this market cannot be published without. Each must have a fetch with a real content digest. SIC 6770 and 6798 are separate datasets of one provider and are required separately.';

-- ------------------------------------------------------- C. ruleset version
create or replace function pipeline.validation_ruleset_version()
returns text
language sql
immutable
set search_path = ''
as $$
  select 'publication-1.1c.0';
$$;

comment on function pipeline.validation_ruleset_version() is
  'The version of the rules pipeline.validate_run applies today. Bump it whenever those rules change, so a publication records which ruleset actually passed it. Publications made under an older ruleset keep the version they were made under.';

grant execute on function pipeline.validation_ruleset_version()
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- ------------------------------------------------- D. two kinds of run link
alter table ref.security_identifiers add column if not exists last_provenance_run_id uuid references pipeline.runs (run_id);
alter table ref.issuer_names add column if not exists last_provenance_run_id uuid references pipeline.runs (run_id);

comment on column ref.security_identifiers.ingestion_run_id is
  'The run that first created this row version. It does not move when a later run merely re-confirms the value.';
comment on column ref.security_identifiers.last_provenance_run_id is
  'The run that last confirmed or corrected source_id / source_record_id / observed_at / available_at for this row.';
comment on column ref.issuer_names.ingestion_run_id is
  'The run that first created this row version.';
comment on column ref.issuer_names.last_provenance_run_id is
  'The run that last confirmed or corrected this row''s provenance.';

-- Backfill: what we know today is that the provenance currently in the row was
-- established by the run that created it. (ref master is not a run artifact, so
-- this is a correction of the master - it is not rewriting a published run.)
update ref.security_identifiers set last_provenance_run_id = ingestion_run_id where last_provenance_run_id is null;
update ref.issuer_names set last_provenance_run_id = ingestion_run_id where last_provenance_run_id is null;

create or replace function ref.refresh_identifier_provenance(p_run_id uuid)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  update ref.security_identifiers si
  set source_id = i.source_id,
      source_record_id = i.source_record_id,
      observed_at = i.observed_at,
      available_at = greatest(si.available_at, i.available_at),
      last_provenance_run_id = p_run_id
  from pipeline.snapshot_identifiers(p_run_id) i
  where si.security_id = i.security_id
    and si.id_type = i.id_type
    and si.id_namespace is not distinct from i.id_namespace
    and si.effective_to is null
    and si.id_value = i.id_value
    and (si.source_id is distinct from i.source_id
      or si.source_record_id is distinct from i.source_record_id
      or si.observed_at is distinct from i.observed_at
      or si.available_at < i.available_at
      or si.last_provenance_run_id is distinct from p_run_id);
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function ref.refresh_identifier_provenance(uuid) is
  'Repairs where an existing identifier says it came from, without opening a new version, and records which run last confirmed that provenance. available_at only moves later.';

create or replace function ref.refresh_issuer_name_matching(p_run_id uuid)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  update ref.issuer_names ins
  set normalized_name = i.normalized_name,
      source_id = i.name_source,
      observed_at = i.observed_at,
      available_at = greatest(ins.available_at, i.available_at),
      last_provenance_run_id = p_run_id
  from pipeline.snapshot_issuers(p_run_id) i
  where ins.issuer_id = i.issuer_id
    and ins.name_type = i.name_type
    and ins.effective_to is null
    and (ins.normalized_name is distinct from i.normalized_name
      or ins.source_id is distinct from i.name_source
      or ins.observed_at is distinct from i.observed_at
      or ins.available_at < i.available_at
      or ins.last_provenance_run_id is distinct from p_run_id);
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

revoke all on function
  ref.refresh_identifier_provenance(uuid),
  ref.refresh_issuer_name_matching(uuid)
from public;
grant execute on function
  ref.refresh_identifier_provenance(uuid),
  ref.refresh_issuer_name_matching(uuid)
to surge_worker_prod;

-- --------------------------------- publication records the ruleset that passed
create or replace function pipeline.publish_run(
  p_run_id uuid,
  p_validation_version text default null
)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_validation jsonb;
  v_recheck jsonb;
  v_run pipeline.runs%rowtype;
  v_supersedes uuid;
  v_version text := coalesce(p_validation_version, pipeline.validation_ruleset_version());
begin
  perform pg_advisory_xact_lock(pipeline.run_lock_key(p_run_id));

  select * into v_run from pipeline.runs where run_id = p_run_id for share;
  if not found then
    raise exception 'run % does not exist', p_run_id;
  end if;

  v_validation := pipeline.validate_run(p_run_id);
  if not (v_validation ->> 'ok')::boolean then
    raise exception 'run % does not validate: %', p_run_id, v_validation ->> 'checks';
  end if;

  select p.run_id into v_supersedes
  from pipeline.run_publications p
  join pipeline.runs r on r.run_id = p.run_id
  where r.market_code = v_run.market_code
    and r.versions ->> 'universe_version' = v_run.versions ->> 'universe_version'
  order by p.published_at desc, p.publication_seq desc
  limit 1;

  insert into pipeline.run_publications (run_id, validation_version, validation_summary, supersedes_run_id)
  values (p_run_id, v_version, v_validation, v_supersedes)
  on conflict (run_id) do update set
    validation_version = excluded.validation_version,
    validation_summary = excluded.validation_summary;

  v_recheck := pipeline.validate_run(p_run_id);
  if (v_recheck -> 'checks') is distinct from (v_validation -> 'checks') then
    raise exception 'run % changed while it was being published; nothing was published', p_run_id;
  end if;

  return jsonb_build_object(
    'published', p_run_id,
    'market_code', v_run.market_code,
    'validation_version', v_version,
    'supersedes', v_supersedes,
    'validation', v_validation
  );
end;
$$;

comment on function pipeline.publish_run(uuid, text) is
  'Publishes a validated run under an exclusive advisory lock on that run, records the validation ruleset version that passed it, and re-validates before returning.';

revoke all on function pipeline.publish_run(uuid, text) from public;
