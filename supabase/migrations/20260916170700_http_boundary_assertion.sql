-- Phase 1.1b: state the HTTP boundary honestly, and check it.
--
-- 20260916170000 tried to revoke EXECUTE on extensions.http* from PUBLIC. On
-- Supabase those functions are owned by supabase_admin and the PUBLIC grant was
-- made by supabase_admin, so the project role (postgres, holding X* but not a
-- member of supabase_admin) cannot revoke it. The REVOKE is a silent no-op:
-- PUBLIC still shows EXECUTE.
--
-- What actually holds the boundary is schema USAGE. A role that cannot use
-- schema `extensions` cannot call anything in it, EXECUTE or not, and every
-- surge_* role had that revoked. Rather than leave a migration that looks like
-- it did something it did not, the invariant is asserted here and by the test
-- suite, and the residual is written down.

create or replace function pipeline.assert_http_boundary()
returns jsonb
language plpgsql
stable
set search_path = ''
as $$
declare
  v_roles text[] := array['surge_worker_prod', 'surge_worker_prod_app', 'surge_worker_research', 'surge_readonly'];
  v_role text;
  v_with_usage text[] := '{}';
  v_public_execute int := 0;
begin
  if not exists (select 1 from pg_namespace where nspname = 'extensions') then
    return jsonb_build_object('extensions_schema', false, 'ok', true);
  end if;

  foreach v_role in array v_roles loop
    if exists (select 1 from pg_roles where rolname = v_role)
       and has_schema_privilege(v_role, 'extensions', 'USAGE') then
      v_with_usage := v_with_usage || v_role;
    end if;
  end loop;

  select count(*) into v_public_execute
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'extensions' and p.proname like 'http%'
    and has_function_privilege('public', p.oid, 'EXECUTE');

  if array_length(v_with_usage, 1) > 0 then
    raise exception 'runtime roles can reach the HTTP client directly: %', v_with_usage;
  end if;

  return jsonb_build_object(
    'extensions_schema', true,
    'runtime_roles_with_usage', v_with_usage,
    'http_functions_public_execute', v_public_execute,
    'note', 'PUBLIC EXECUTE on extensions.http* belongs to supabase_admin and cannot be revoked by this role; '
            'schema USAGE is the control, and no runtime role has it',
    'ok', true
  );
end;
$$;

comment on function pipeline.assert_http_boundary() is
  'Fails if any runtime role can reach schema extensions. The HTTP client is only usable through pipeline.load_master_snapshot_from_signed_url, which is SECURITY DEFINER and checks the allowlist first.';

grant execute on function pipeline.assert_http_boundary()
  to surge_worker_prod, surge_worker_research, surge_readonly;

do $$
declare
  v_result jsonb;
begin
  v_result := pipeline.assert_http_boundary();
  raise notice 'http boundary: %', v_result;
end
$$;
