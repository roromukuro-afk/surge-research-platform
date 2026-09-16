-- Phase 1: schemas, enum types, application roles.
-- Source of truth for all schema changes is this directory (no manual dashboard DDL).

create schema if not exists ref;
create schema if not exists pipeline;
create schema if not exists universe;
create schema if not exists prod;
create schema if not exists research;

comment on schema ref is 'Security master and reference data.';
comment on schema pipeline is 'Ingestion runs, sources, provenance, errors, coverage inputs.';
comment on schema universe is 'Universe definitions, evaluations and coverage.';
comment on schema prod is 'Production-only outputs. Research jobs must never write here.';
comment on schema research is 'Research / replay outputs. Never mixed with production results.';

-- ---------------------------------------------------------------- enum types
do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'market_code' and n.nspname = 'ref') then
    create type ref.market_code as enum ('JP', 'US');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'security_type' and n.nspname = 'ref') then
    create type ref.security_type as enum (
      'COMMON_STOCK',
      'FOREIGN_COMMON_STOCK',
      'ADR',
      'PREFERRED',
      'ETF',
      'ETN',
      'REIT_OR_FUND',
      'WARRANT',
      'RIGHT',
      'UNIT',
      'INVESTMENT_CERTIFICATE',
      'OTHER',
      'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'listing_status' and n.nspname = 'ref') then
    create type ref.listing_status as enum ('LISTED', 'DELISTED', 'SUSPENDED', 'UNKNOWN');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'run_mode' and n.nspname = 'pipeline') then
    create type pipeline.run_mode as enum ('PRODUCTION', 'RESEARCH', 'DEV');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'run_status' and n.nspname = 'pipeline') then
    create type pipeline.run_status as enum ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'decision' and n.nspname = 'universe') then
    create type universe.decision as enum ('INCLUDED', 'EXCLUDED', 'UNRESOLVED');
  end if;
end
$$;

-- -------------------------------------------------------------------- roles
-- NOLOGIN group roles. Credentials are never created or stored in migrations.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'surge_worker_prod') then
    create role surge_worker_prod nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'surge_worker_research') then
    create role surge_worker_research nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'surge_readonly') then
    create role surge_readonly nologin;
  end if;
end
$$;

-- Role intent (COMMENT ON ROLE needs superuser on managed Postgres, so it is documented here):
--   surge_worker_prod     : production ingestion/analysis jobs, read-write in ref/pipeline/universe/prod
--   surge_worker_research : research jobs, read-only outside the research schema, no access to prod
--   surge_readonly        : read-only consumer (web/API)

-- Deny by default everywhere, then grant explicitly in the grants migration.
revoke all on schema ref, pipeline, universe, prod, research from public;

grant usage on schema ref, pipeline, universe to surge_worker_prod, surge_worker_research, surge_readonly;
grant usage on schema prod to surge_worker_prod, surge_readonly;
grant usage on schema research to surge_worker_prod, surge_worker_research, surge_readonly;

-- surge_worker_research must not be able to create or write anything in prod.
revoke all on schema prod from surge_worker_research;
