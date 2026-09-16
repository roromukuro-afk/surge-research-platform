-- Phase 1: pin the search_path of every function.
--
-- A mutable search_path lets a caller's session decide which objects a function
-- touches. Every function here already uses fully qualified names, so the path
-- can simply be empty.

alter function ref.deterministic_uuid(text) set search_path = '';
alter function ref.listings_as_of(timestamptz) set search_path = '';
alter function ref.apply_master_snapshot(uuid) set search_path = '';
alter function universe.apply_snapshot_evaluations(uuid, text, date) set search_path = '';
alter function universe.compute_coverage(uuid, text, date, jsonb, text) set search_path = '';

-- Installed only where the http extension exists (see the bulk load migration).
do $$
begin
  if exists (
    select 1
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'pipeline' and p.proname = 'load_master_snapshot_from_url'
  ) then
    execute 'alter function pipeline.load_master_snapshot_from_url(uuid, text, text, integer) set search_path = ''''';
  end if;
end
$$;
