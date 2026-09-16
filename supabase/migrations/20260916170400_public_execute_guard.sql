-- Phase 1.1b: make "no function is public" hold by construction.
--
-- 20260916170100 used ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ON FUNCTIONS
-- FROM PUBLIC. Measured on both Postgres 17 targets (the cloud project and the
-- CI database), a bare REVOKE stores nothing: there is no default ACL entry to
-- revoke from, so a newly created function still came out PUBLIC executable.
--
-- Two mechanisms, because this must not depend on one subtlety:
--   1. an explicit positive default first, so the REVOKE has an entry to modify;
--   2. an event trigger that strips PUBLIC from every function created in these
--      schemas, whoever creates it and whatever the defaults say.
--
-- The invariant is asserted in the test suite, so a future migration that adds a
-- function without thinking about privileges still cannot make one public.

-- ------------------------------------------------- 1. a default that persists
do $$
declare
  schema_name text;
begin
  foreach schema_name in array array['ref', 'pipeline', 'universe', 'prod', 'research']
  loop
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

-- --------------------------------------------------- 2. the event trigger
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
       and split_part(obj.object_identity, '.', 1) in ('ref', 'pipeline', 'universe', 'prod', 'research')
    then
      execute format('revoke all on function %s from public', obj.objid::regprocedure);
    end if;
  end loop;
end;
$$;

comment on function pipeline.revoke_public_execute_on_new_functions() is
  'Event trigger body: strips the Postgres default of EXECUTE for PUBLIC from every function created in the project schemas. A migration that forgets to revoke cannot leave one open.';

do $$
begin
  execute 'drop event trigger if exists surge_revoke_public_execute';
  execute 'create event trigger surge_revoke_public_execute on ddl_command_end '
          'when tag in (''CREATE FUNCTION'', ''CREATE PROCEDURE'') '
          'execute function pipeline.revoke_public_execute_on_new_functions()';
  raise notice 'event trigger surge_revoke_public_execute installed';
exception
  when insufficient_privilege then
    -- A managed environment may not allow event triggers. The default above and
    -- the sweep below still apply, and the test suite still asserts the result.
    raise notice 'event triggers are not permitted here: relying on default privileges and the test guard';
end
$$;

-- ---------------------------------------- 3. sweep anything already public
do $$
declare
  fn record;
begin
  for fn in
    select p.oid::regprocedure as signature
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname in ('ref', 'pipeline', 'universe', 'prod', 'research')
      and has_function_privilege('public', p.oid, 'EXECUTE')
  loop
    execute format('revoke all on function %s from public', fn.signature);
  end loop;
end
$$;
