-- Keep the web principal's reach inside the ui schema.
--
-- The contract smoke test found this, which is what it is for. ui.unfilled_roles
-- read market.unfilled_roles, which calls market.provider_for_role(). Postgres
-- checks TABLE privileges for a view against the view's owner, but it checks
-- FUNCTION execute privileges against the CALLER - so the view worked for a
-- researcher and failed for surge_web with "permission denied for function
-- provider_for_role".
--
-- The fix keeps the privilege surface where it was designed to be. Granting
-- surge_web execute on a market function would have worked and would have made
-- "the application touches nothing outside ui" false, one grant at a time.
--
-- The role list comes from the catalog rather than from enum_range() for the
-- same reason: pg_enum is world-readable, so nothing here depends on a privilege
-- outside the contract.

create or replace view ui.unfilled_roles as
  select
    -- Cast and collate, both required. enumlabel is `name`, and CREATE OR
    -- REPLACE VIEW can change neither the type nor the collation of an existing
    -- column. `name` carries the "C" collation, which a bare ::text inherits, so
    -- the replacement is rejected without the explicit COLLATE.
    label.enumlabel::text collate "default" as role,
    'market data'::text as domain,
    'no provider bound'::text as detail
  from pg_catalog.pg_enum label
  join pg_catalog.pg_type typ on typ.oid = label.enumtypid
  join pg_catalog.pg_namespace nsp on nsp.oid = typ.typnamespace
  where nsp.nspname = 'market'
    and typ.typname = 'provider_role'
    and not exists (
      select 1
      from market.provider_role_bindings binding
      where binding.role::text = label.enumlabel
        and binding.binding_version = 'bindings-1.0.0'
        and binding.enabled
        and binding.effective_to is null
    )
  union all
  select
    'NEWS_' || source.scope::text,
    'news'::text,
    'enabled, but never fetched from the real service'::text
  from news.sources source
  where source.enabled = true and source.live_verified_at is null
  group by source.scope;

comment on view ui.unfilled_roles is
  'Roles the platform cannot fill today. Kept on a screen rather than in a report, because an unfilled role is invisible until someone asks why a number is zero. Reads only tables and catalogs, so the web principal needs no privilege outside the ui schema.';

grant select on ui.unfilled_roles to surge_web, surge_readonly, surge_worker_research;
