-- Phase 1.1b: which run is the universe? Publication decides, not recency.
--
-- Several SUCCEEDED runs exist for the same market and as-of date (a re-fetch, a
-- rebuild), plus one that never finished. "Latest finished_at" is not an answer:
-- it would let a half-validated re-run silently become the universe, and it
-- would let a run built today answer a question about what was known last week.
--
-- A run becomes authoritative only when it is PUBLISHED, and a reader always
-- asks for the publication that existed at its knowledge cutoff.

-- --------------------------------------------- tidy the run that never ended
do $$
declare
  v_run record;
begin
  for v_run in
    select run_id, market_code, started_at
    from pipeline.runs
    where status = 'RUNNING' and started_at < now() - interval '1 hour'
  loop
    insert into pipeline.run_errors (run_id, stage, severity, error_type, message, context)
    values (
      v_run.run_id, 'run_lifecycle', 'ERROR', 'RUN_ABANDONED',
      'run left RUNNING with no completion; closed as FAILED by 20260916170300',
      jsonb_build_object('started_at', v_run.started_at, 'closed_by', 'phase-1.1b')
    );
    update pipeline.runs set status = 'FAILED', finished_at = now() where run_id = v_run.run_id;
  end loop;
end
$$;

-- --------------------------------------------------------- run provenance
alter table pipeline.runs add column if not exists config_hash text;

comment on column pipeline.runs.config_hash is
  'Fingerprint of the settings that can change the result (providers and endpoints, paging limits, universe / identity / job versions). A production run without one cannot be reproduced.';

-- Existing rows predate the requirement, so the constraint is NOT VALID: it
-- binds every new row without rewriting history into something it was not.
alter table pipeline.runs drop constraint if exists runs_production_provenance_ck;
alter table pipeline.runs add constraint runs_production_provenance_ck
  check (
    run_mode <> 'PRODUCTION'
    or (git_sha is not null and config_hash is not null and job_version is not null)
  ) not valid;

comment on constraint runs_production_provenance_ck on pipeline.runs is
  'A PRODUCTION run must carry the commit and the configuration it came from. NOT VALID: rows written before Phase 1.1b are left as they are rather than back-filled with invented provenance.';

-- ------------------------------------------------------------- publications
create table if not exists pipeline.run_publications (
  run_id              uuid primary key references pipeline.runs (run_id),
  published_at        timestamptz not null default now(),
  validation_version  text not null,
  validation_summary  jsonb not null default '{}'::jsonb,
  supersedes_run_id   uuid references pipeline.runs (run_id),
  published_by        text not null default current_user,
  created_at          timestamptz not null default now()
);

comment on table pipeline.run_publications is
  'A run becomes authoritative when it is published here. published_at is the knowledge time of that decision: a run published today never answers a question about what was known yesterday.';

create index if not exists run_publications_published_idx
  on pipeline.run_publications (published_at desc);

grant select on pipeline.run_publications to surge_worker_prod, surge_worker_research, surge_readonly;

alter table pipeline.run_publications enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname='pipeline' and tablename='run_publications' and policyname='surge_worker_prod_read') then
    create policy surge_worker_prod_read on pipeline.run_publications as permissive for select to surge_worker_prod using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='pipeline' and tablename='run_publications' and policyname='surge_worker_research_read') then
    create policy surge_worker_research_read on pipeline.run_publications as permissive for select to surge_worker_research using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname='pipeline' and tablename='run_publications' and policyname='surge_readonly_read') then
    create policy surge_readonly_read on pipeline.run_publications as permissive for select to surge_readonly using (true);
  end if;
end
$$;

-- --------------------------------------------------------------- validation
create or replace function pipeline.validate_run(p_run_id uuid)
returns jsonb
language plpgsql
stable
set search_path = ''
as $$
declare
  v_run pipeline.runs%rowtype;
  v_checks jsonb := '{}'::jsonb;
  v_coverage record;
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
    'market_coverage_present', v_coverage is not null,
    'no_fatal_errors', v_fatal = 0,
    'snapshot_rows', v_snapshot,
    'evaluation_rows', v_evaluations,
    'coverage_retrieved', coalesce(v_coverage.retrieved_count, -1),
    'coverage_unique', coalesce(v_coverage.unique_count, -1),
    'counts_consistent',
      v_coverage is not null
      and v_coverage.retrieved_count = v_snapshot
      and v_evaluations = v_coverage.unique_count
      and (v_coverage.included_count + v_coverage.excluded_count + v_coverage.unresolved_count) = v_coverage.retrieved_count
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

comment on function pipeline.validate_run(uuid) is
  'Everything a run must satisfy before it may be published: finished, attributable, complete, internally consistent and free of fatal errors.';

create or replace function pipeline.publish_run(
  p_run_id uuid,
  p_validation_version text default 'publication-1.0.0'
)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_validation jsonb;
  v_run pipeline.runs%rowtype;
  v_supersedes uuid;
begin
  v_validation := pipeline.validate_run(p_run_id);
  if not (v_validation ->> 'ok')::boolean then
    raise exception 'run % does not validate: %', p_run_id, v_validation ->> 'checks';
  end if;

  select * into v_run from pipeline.runs where run_id = p_run_id;

  select p.run_id into v_supersedes
  from pipeline.run_publications p
  join pipeline.runs r on r.run_id = p.run_id
  where r.market_code = v_run.market_code
    and r.versions ->> 'universe_version' = v_run.versions ->> 'universe_version'
  order by p.published_at desc
  limit 1;

  insert into pipeline.run_publications (run_id, validation_version, validation_summary, supersedes_run_id)
  values (p_run_id, p_validation_version, v_validation, v_supersedes)
  on conflict (run_id) do update set
    validation_version = excluded.validation_version,
    validation_summary = excluded.validation_summary;

  return jsonb_build_object(
    'published', p_run_id,
    'market_code', v_run.market_code,
    'supersedes', v_supersedes,
    'validation', v_validation
  );
end;
$$;

comment on function pipeline.publish_run(uuid, text) is
  'Publishes a validated run. Refuses anything that does not pass pipeline.validate_run, so an unfinished or unattributable run can never become the universe.';

-- ------------------------------------------------------- authoritative reads
create or replace function universe.authoritative_run_at(
  p_market_code ref.market_code,
  p_universe_version text,
  p_knowledge_cutoff timestamptz default now()
)
returns uuid
language sql
stable
set search_path = ''
as $$
  select p.run_id
  from pipeline.run_publications p
  join pipeline.runs r on r.run_id = p.run_id
  where r.market_code = p_market_code
    and r.versions ->> 'universe_version' = p_universe_version
    and p.published_at <= p_knowledge_cutoff
  order by p.published_at desc, r.finished_at desc
  limit 1;
$$;

comment on function universe.authoritative_run_at(ref.market_code, text, timestamptz) is
  'The run that was authoritative at a knowledge cutoff: the latest publication at or before it. A run published later never answers for an earlier moment, so a replay cannot see a universe that was rebuilt afterwards.';

create or replace function universe.eligibility_as_of(
  p_market_code ref.market_code,
  p_universe_version text,
  p_knowledge_cutoff timestamptz default now()
)
returns table (
  security_id uuid,
  listing_id uuid,
  decision universe.decision,
  reason_code text,
  as_of_date date,
  run_id uuid
)
language sql
stable
set search_path = ''
as $$
  select e.security_id, e.listing_id, e.decision, e.reason_code, e.as_of_date, e.run_id
  from universe.evaluations e
  where e.run_id = universe.authoritative_run_at(p_market_code, p_universe_version, p_knowledge_cutoff)
    and e.universe_version = p_universe_version;
$$;

comment on function universe.eligibility_as_of(ref.market_code, text, timestamptz) is
  'Universe decisions from the authoritative run at a knowledge cutoff. This is what downstream phases read; universe.evaluations holds every run, published or not.';

create or replace view universe.current_eligibility as
  select r.market_code,
         e.universe_version,
         e.as_of_date,
         e.security_id,
         e.listing_id,
         e.decision,
         e.reason_code,
         e.run_id,
         p.published_at
  from pipeline.run_publications p
  join pipeline.runs r on r.run_id = p.run_id
  join universe.evaluations e on e.run_id = p.run_id
  where p.run_id = universe.authoritative_run_at(
          r.market_code, e.universe_version, now());

comment on view universe.current_eligibility is
  'The current universe: evaluations from the latest published run per market and universe version.';

grant select on universe.current_eligibility to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function
  universe.authoritative_run_at(ref.market_code, text, timestamptz),
  universe.eligibility_as_of(ref.market_code, text, timestamptz)
to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function pipeline.validate_run(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;
-- Publishing is an operator decision, not a runtime one.
revoke all on function pipeline.publish_run(uuid, text) from public;
