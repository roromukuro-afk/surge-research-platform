-- Phase 1.1b: a new function is not public by default.
--
-- Postgres grants EXECUTE on a new function to PUBLIC. 20260916160800 revoked it
-- from the functions that existed at the time, and two functions created later
-- in the same migration immediately had it again - which is why 20260916160900
-- existed. Revoking per function does not scale and will be forgotten.
--
-- The default itself is changed for the role that owns the migrations, so a
-- function added by a later migration is private unless it says otherwise.

alter default privileges in schema ref, pipeline, universe, prod, research
  revoke execute on functions from public;

-- Belt and braces for anything that slipped in between 160900 and here.
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
