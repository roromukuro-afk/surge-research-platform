-- Phase 1.1c: a published run cannot be edited afterwards.
--
-- The publication model decided WHICH run is the universe, but the run's own
-- artifacts stayed writable: the worker could still update universe.evaluations
-- or insert into pipeline.run_errors for a run that a historical replay already
-- depends on. A published result that can change is not a result.
--
-- Rule: before publication a run is built freely; after publication every row
-- belonging to it is frozen, for every role, including the owner. Correcting a
-- published run means publishing a new one that supersedes it - which is what
-- the publication model is for.
--
-- Publication and artifact writes are serialised through an advisory lock keyed
-- on the run, so validation cannot pass while another transaction is still
-- changing the same run's rows.

create or replace function pipeline.run_lock_key(p_run_id uuid)
returns bigint
language sql
immutable
set search_path = ''
as $$
  select ('x' || substr(md5('pipeline.run:' || p_run_id::text), 1, 16))::bit(64)::bigint;
$$;

comment on function pipeline.run_lock_key(uuid) is
  'Advisory lock key for one run. Artifact writers take it in shared mode, publication takes it exclusively.';

create or replace function pipeline.assert_run_mutable(p_run_id uuid)
returns void
language plpgsql
set search_path = ''
as $$
begin
  if p_run_id is null then
    return;
  end if;

  -- Shared: several writers may build the same run at once, but a publication
  -- (exclusive) cannot slip between a validation and its insert.
  perform pg_advisory_xact_lock_shared(pipeline.run_lock_key(p_run_id));

  if exists (select 1 from pipeline.run_publications where run_id = p_run_id) then
    raise exception
      'run % is published and its artifacts are immutable; publish a new run that supersedes it instead',
      p_run_id
      using errcode = 'read_only_sql_transaction';
  end if;
end;
$$;

comment on function pipeline.assert_run_mutable(uuid) is
  'Raises if the run has been published. Called by the freeze triggers on every artifact table.';

create or replace function pipeline.freeze_published_run_artifacts()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    perform pipeline.assert_run_mutable(new.run_id);
    return new;
  elsif tg_op = 'UPDATE' then
    perform pipeline.assert_run_mutable(old.run_id);
    perform pipeline.assert_run_mutable(new.run_id);
    return new;
  else
    perform pipeline.assert_run_mutable(old.run_id);
    return old;
  end if;
end;
$$;

comment on function pipeline.freeze_published_run_artifacts() is
  'Row trigger for tables whose rows belong to a run. INSERT, UPDATE and DELETE are all refused once that run is published.';

-- pipeline.runs is keyed on run_id itself, so it needs its own function.
create or replace function pipeline.freeze_published_run()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' then
    perform pipeline.assert_run_mutable(old.run_id);
    if new.run_id is distinct from old.run_id then
      perform pipeline.assert_run_mutable(new.run_id);
    end if;
    return new;
  end if;
  perform pipeline.assert_run_mutable(old.run_id);
  return old;
end;
$$;

comment on function pipeline.freeze_published_run() is
  'Row trigger on pipeline.runs: a published run row cannot be updated or deleted.';

do $$
declare
  t record;
begin
  for t in
    select * from (values
      ('pipeline', 'source_fetches'),
      ('pipeline', 'run_errors'),
      ('pipeline', 'master_snapshot'),
      ('universe', 'evaluations'),
      ('universe', 'coverage')
    ) as v(schema_name, table_name)
  loop
    execute format('drop trigger if exists freeze_published_run on %I.%I', t.schema_name, t.table_name);
    execute format(
      'create trigger freeze_published_run before insert or update or delete on %I.%I '
      'for each row execute function pipeline.freeze_published_run_artifacts()',
      t.schema_name, t.table_name
    );
  end loop;

  execute 'drop trigger if exists freeze_published_run on pipeline.runs';
  execute 'create trigger freeze_published_run before update or delete on pipeline.runs '
          'for each row execute function pipeline.freeze_published_run()';
end
$$;

-- The publication itself takes the lock exclusively, then validates, then
-- writes. A writer holding the shared lock blocks it; a writer arriving after it
-- waits, and then finds the run published and is refused.
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
  v_recheck jsonb;
  v_run pipeline.runs%rowtype;
  v_supersedes uuid;
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
  values (p_run_id, p_validation_version, v_validation, v_supersedes)
  on conflict (run_id) do update set
    validation_version = excluded.validation_version,
    validation_summary = excluded.validation_summary;

  -- What is published is what was validated: if anything moved in between, the
  -- publication is abandoned rather than recorded against different content.
  v_recheck := pipeline.validate_run(p_run_id);
  if (v_recheck -> 'checks') is distinct from (v_validation -> 'checks') then
    raise exception 'run % changed while it was being published; nothing was published', p_run_id;
  end if;

  return jsonb_build_object(
    'published', p_run_id,
    'market_code', v_run.market_code,
    'supersedes', v_supersedes,
    'validation', v_validation
  );
end;
$$;

comment on function pipeline.publish_run(uuid, text) is
  'Publishes a validated run under an exclusive advisory lock on that run, and re-validates before returning: the publication records exactly the content that was validated.';

revoke all on function pipeline.publish_run(uuid, text) from public;
grant execute on function
  pipeline.run_lock_key(uuid),
  pipeline.assert_run_mutable(uuid)
to surge_worker_prod, surge_worker_research, surge_readonly;
