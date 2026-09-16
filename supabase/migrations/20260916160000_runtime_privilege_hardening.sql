-- Phase 1.1a: the runtime worker may not rewrite its own configuration.
--
-- 20260916120600 granted `select, insert, update` on ALL tables in ref, pipeline
-- and universe to surge_worker_prod, and set ALTER DEFAULT PRIVILEGES so that
-- every table created later inherits the same write grant. That is how
-- pipeline.load_host_allowlist - the control that stops the database fetching a
-- snapshot from an arbitrary host - ended up insert/update-able by the very role
-- it is supposed to constrain, even though 20260916150200 granted it SELECT only.
--
-- Three changes here:
--   1. configuration and reference data become read-only at runtime;
--   2. new tables are no longer born writable, so the same defect cannot be
--      re-introduced by the next migration that adds a table;
--   3. production data stays append-only (no DELETE, now also by default).
--
-- Run output (runs, fetches, errors, staging, the ref master and its history,
-- universe evaluations and coverage) stays writable: that is what the worker is
-- for. Every table the worker may write is listed explicitly at the end.

-- ------------------------------------------------- 1. configuration is read-only
revoke insert, update, delete on
  pipeline.sources,
  pipeline.load_host_allowlist,
  ref.exchanges,
  universe.definitions,
  universe.decision_reasons,
  ref.identity_migration_map
from surge_worker_prod, surge_worker_research;

-- SELECT stays: the worker reads its source registry, the exchange list, the
-- universe definition and the reason vocabulary on every run.
grant select on
  pipeline.sources,
  pipeline.load_host_allowlist,
  ref.exchanges,
  universe.definitions,
  universe.decision_reasons,
  ref.identity_migration_map
to surge_worker_prod, surge_worker_research, surge_readonly;

comment on table pipeline.load_host_allowlist is
  'Hosts the database may fetch bulk snapshots from. Empty by default, changed only by a migration or a database administrator: the runtime worker has SELECT only, so it cannot add a host to its own allowlist.';
comment on table ref.identity_migration_map is
  'Old to new identifier mapping for documented rebuilds. Written by the rebuild procedure (ref.finalize_identity_rebuild, run by an administrator), read-only for the runtime worker.';

-- ---------------------------------------- 2. new tables are not born writable
-- From now on a migration that adds a runtime table must grant write to
-- surge_worker_prod explicitly. A new configuration table therefore defaults to
-- read-only instead of silently becoming writable.
alter default privileges in schema ref, pipeline, universe
  revoke insert, update on tables from surge_worker_prod;
alter default privileges in schema ref, pipeline, universe
  grant select on tables to surge_worker_prod;

-- --------------------------------------------- 3. production stays append-only
revoke delete on all tables in schema prod from surge_worker_prod;
alter default privileges in schema prod
  revoke delete on tables from surge_worker_prod;

-- --------------------------------------------------------------- RLS policies
-- The blanket `for all` policy on the configuration tables is replaced by a
-- SELECT-only policy, so RLS agrees with the grants instead of contradicting
-- them. (Grants are the effective control; this removes the misleading policy.)
do $$
declare
  t record;
begin
  for t in
    select * from (values
      ('pipeline', 'sources'),
      ('pipeline', 'load_host_allowlist'),
      ('ref', 'exchanges'),
      ('ref', 'identity_migration_map'),
      ('universe', 'definitions'),
      ('universe', 'decision_reasons')
    ) as v(schemaname, tablename)
  loop
    execute format('drop policy if exists surge_worker_prod_all on %I.%I', t.schemaname, t.tablename);
    if not exists (
      select 1 from pg_policies p
      where p.schemaname = t.schemaname and p.tablename = t.tablename
        and p.policyname = 'surge_worker_prod_read'
    ) then
      execute format(
        'create policy surge_worker_prod_read on %I.%I as permissive for select to surge_worker_prod using (true)',
        t.schemaname, t.tablename
      );
    end if;
  end loop;
end
$$;

-- ------------------------------------------------- the runtime write surface
-- Explicit, so that "what may the worker write" is answerable from git.
grant select, insert, update on
  pipeline.runs,
  pipeline.source_fetches,
  pipeline.run_errors,
  pipeline.master_snapshot,
  ref.issuers,
  ref.securities,
  ref.listings,
  ref.listing_states,
  ref.listing_symbols,
  ref.security_names,
  ref.security_identifiers,
  universe.evaluations,
  universe.coverage
to surge_worker_prod;
