-- Phase 2.1: one list of project schemas, so a new one cannot escape the guards.
--
-- 20260916170400 stripped PUBLIC EXECUTE from new functions, but it named the
-- schemas in three separate places. Adding the market schema left every function
-- in it publicly executable - the CI test caught it, which is the only reason
-- this is a footnote rather than an incident.
--
-- The list is now a function. The event trigger, the sweep and the test all read
-- it, so the next schema is protected by being added in one place instead of
-- three, and the test fails loudly if it is added in none.

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market'];
$$;

comment on function pipeline.project_schemas() is
  'Every schema this project owns. The privilege guards and the test suite read this, so a schema added here is protected everywhere and a schema added anywhere else is caught.';

grant execute on function pipeline.project_schemas()
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- ------------------------------------------- 1. defaults, for every schema
do $$
declare
  schema_name text;
begin
  foreach schema_name in array pipeline.project_schemas()
  loop
    -- The positive grant first: a bare REVOKE has no default ACL entry to
    -- modify and silently stores nothing (measured on Postgres 17).
    execute format(
      'alter default privileges in schema %I grant execute on functions to %I',
      schema_name, current_user
    );
    execute format(
      'alter default privileges in schema %I revoke execute on functions from public',
      schema_name
    );
  end loop;
end
$$;

-- ---------------------------------------------- 2. the event trigger body
create or replace function pipeline.revoke_public_execute_on_new_functions()
returns event_trigger
language plpgsql
as $$
declare
  obj record;
begin
  for obj in select * from pg_event_trigger_ddl_commands()
  loop
    if obj.command_tag in ('CREATE FUNCTION', 'CREATE PROCEDURE')
       and split_part(obj.object_identity, '.', 1) = any (pipeline.project_schemas())
    then
      execute format('revoke all on function %s from public', obj.objid::regprocedure);
    end if;
  end loop;
end;
$$;

comment on function pipeline.revoke_public_execute_on_new_functions() is
  'Event trigger body: strips the Postgres default of EXECUTE for PUBLIC from every function created in a project schema. The schema list comes from pipeline.project_schemas().';

-- -------------------------------------- 3. sweep whatever is already public
do $$
declare
  fn record;
begin
  for fn in
    select p.oid::regprocedure as signature
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = any (pipeline.project_schemas())
      and has_function_privilege('public', p.oid, 'EXECUTE')
  loop
    execute format('revoke all on function %s from public', fn.signature);
  end loop;
end
$$;

-- The market functions each have their intended grantees already; the sweep
-- above only removes PUBLIC. Re-state the two that the purge principal needs,
-- in case the sweep ran before they were granted.
grant execute on function
  market.open_purge_request(text, text, text, text, text, boolean),
  market.record_purge_result(bigint, text, market.purge_status, text),
  market.complete_purge_request(bigint),
  market.purge_market_rows(bigint)
to surge_purge;

grant execute on function
  market.current_license_policy(text, text),
  market.assert_license_allows(text, text, text),
  market.plan_rank(text, text),
  market.usdjpy_as_of(timestamptz, text, text)
to surge_worker_prod, surge_worker_research, surge_readonly;
