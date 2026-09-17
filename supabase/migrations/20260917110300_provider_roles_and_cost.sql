-- Phase 2: which provider fills which role, and what each one costs to keep.
--
-- The core system has to run at zero recurring cost. That is a constraint on
-- the architecture, not a preference, so it is expressed in the schema: every
-- provider declares a cost class, every role declares which provider fills it,
-- and a role filled by an OPTIONAL_PAID provider is disabled unless someone has
-- deliberately enabled it.
--
-- No single provider has to cover everything. Current prices can come from one
-- source, deep history from another, corporate actions from a third and the
-- delisting record from a fourth - which is exactly what free sources tend to
-- look like. The role table is what keeps that from becoming a tangle: each
-- role names one provider, and every stored row names the provider it came
-- from, so a role that is later refilled leaves the old data readable.

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'provider_cost_class' and n.nspname = 'market') then
    create type market.provider_cost_class as enum (
      'FREE',                   -- no account, no fee
      'FREE_ACCOUNT_REQUIRED',  -- an account is needed but nothing recurring is paid
      'OPTIONAL_PAID',          -- a subscription. Never required for the core to run.
      'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'provider_role' and n.nspname = 'market') then
    -- Roles, not products. A provider is assigned to a role; the code asks for
    -- the role. Swapping the provider behind a role changes no caller.
    create type market.provider_role as enum (
      'SECURITY_MASTER_JP',
      'SECURITY_MASTER_US',
      'EOD_CURRENT_JP',        -- today's / yesterday's close, for daily screening
      'EOD_CURRENT_US',
      'EOD_HISTORY_JP',        -- deep history, for backfill. May be a different source.
      'EOD_HISTORY_US',
      'CORPORATE_ACTIONS_JP',
      'CORPORATE_ACTIONS_US',
      'DELISTING_HISTORY_JP',
      'DELISTING_HISTORY_US',
      'FX_USDJPY',
      'IDENTIFIER_ENRICHMENT_US'
    );
  end if;
end;
$$;

-- ------------------------------------------------------------- the providers
create table if not exists market.providers (
  provider_id text primary key,
  display_name text not null,
  cost_class market.provider_cost_class not null,
  monthly_cost_jpy numeric(12, 2) not null default 0,
  monthly_cost_note text,
  requires_credential boolean not null default false,
  credential_env_var text,
  official_url text,
  notes text
);

comment on table market.providers is
  'Every data source the platform can talk to, and what keeping it costs. monthly_cost_jpy is the RECURRING cost: the number that must stay zero across everything the core system depends on.';
comment on column market.providers.credential_env_var is
  'The environment variable the worker reads the credential from. The NAME lives here; the value never does, and never reaches this database.';

insert into market.providers
  (provider_id, display_name, cost_class, monthly_cost_jpy, monthly_cost_note,
   requires_credential, credential_env_var, official_url, notes)
values
  ('ecb', 'European Central Bank euro reference rates', 'FREE', 0,
   'Published free of charge; no account, no key.', false, null,
   'https://data.ecb.europa.eu/', 'USD/JPY is derived from the USD and JPY euro legs.'),
  ('openfigi', 'OpenFIGI (Bloomberg / OMG)', 'FREE', 0,
   'Free and keyless at 25 requests/minute; a free key raises the limit.', false, 'SURGE_OPENFIGI_API_KEY',
   'https://www.openfigi.com/api', 'FIGI identifiers are dedicated to the public domain.'),
  ('nasdaq_trader_symbol_directory', 'Nasdaq Trader symbol directory', 'FREE', 0,
   'Public directory files; no account.', false, null,
   'https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs',
   'Already the Phase 1 US security master. Also the free basis for detecting US delistings by diffing daily snapshots.'),
  ('sec_company_tickers', 'SEC EDGAR company tickers and SIC directory', 'FREE', 0,
   'Public US federal government data; a declared User-Agent is required.', false, 'SURGE_CONTACT_EMAIL',
   'https://www.sec.gov/search-filings/edgar-application-programming-interfaces', null),
  ('jpx_listed_issues', 'JPX listed issue list', 'FREE', 0,
   'Published by the exchange free of charge.', false, null,
   'https://www.jpx.co.jp/markets/statistics-equities/misc/01.html', null),
  ('edinet_code_list', 'EDINET code list', 'FREE', 0,
   'Published by the FSA free of charge.', false, null, 'https://disclosure2.edinet-fsa.go.jp/', null),
  ('jquants', 'J-Quants API (JPX Market Innovation & Research)', 'OPTIONAL_PAID', 3300,
   'Standard plan, 3,300 JPY/month tax included. NOT required for the core system to run.',
   true, 'SURGE_JQUANTS_API_KEY', 'https://jpx-jquants.com/',
   'Optional paid challenger. The free plan exists but its daily bars are delayed 12 weeks, which is unusable for current screening.'),
  ('eodhd', 'EODHD (EOD Historical Data)', 'OPTIONAL_PAID', 3000,
   'EOD Historical Data - All World, 19.99 USD/month. NOT required for the core system to run.',
   true, 'SURGE_EODHD_API_TOKEN', 'https://eodhd.com/',
   'Optional paid challenger. Its free tier is one year of history at 20 API calls a day, which cannot cover the US universe daily.')
on conflict (provider_id) do update set
  cost_class = excluded.cost_class,
  monthly_cost_jpy = excluded.monthly_cost_jpy,
  monthly_cost_note = excluded.monthly_cost_note,
  notes = excluded.notes;

-- ----------------------------------------------------------------- the roles
create table if not exists market.provider_role_bindings (
  binding_version text not null,
  role market.provider_role not null,
  provider_id text not null references market.providers (provider_id),
  dataset_key text,
  enabled boolean not null default true,
  priority integer not null default 1,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  notes text,
  primary key (binding_version, role, provider_id)
);

comment on table market.provider_role_bindings is
  'Which provider fills which role, versioned. Several providers may fill one role at different priorities - a free source first, a paid one behind it - and the run records which binding was in force.';
comment on column market.provider_role_bindings.priority is
  'Lower is tried first. This is how a free source leads and an optional paid one sits behind it as a challenger rather than as a dependency.';

create index if not exists provider_role_bindings_current_idx
  on market.provider_role_bindings (role, priority) where effective_to is null and enabled;

-- The roles the core fills today. Roles with no free provider yet are recorded
-- as UNFILLED by their absence, which market.unfilled_roles makes visible -
-- silence would let a gap look like a decision.
insert into market.provider_role_bindings (binding_version, role, provider_id, dataset_key, enabled, priority, notes)
values
  ('bindings-1.0.0', 'SECURITY_MASTER_JP', 'jpx_listed_issues', null, true, 1, null),
  ('bindings-1.0.0', 'SECURITY_MASTER_US', 'nasdaq_trader_symbol_directory', null, true, 1, null),
  ('bindings-1.0.0', 'DELISTING_HISTORY_US', 'nasdaq_trader_symbol_directory', null, true, 1,
   'Delistings are detected by diffing consecutive daily directory snapshots; the directory itself publishes no delisting date.'),
  ('bindings-1.0.0', 'FX_USDJPY', 'ecb', 'ECB_EXR_DAILY', true, 1, null),
  ('bindings-1.0.0', 'IDENTIFIER_ENRICHMENT_US', 'openfigi', 'OPENFIGI_MAPPING', true, 1, null),
  -- Optional paid, disabled. Enabling one is a deliberate act with a cost.
  ('bindings-1.0.0', 'EOD_CURRENT_JP', 'jquants', 'JQ_EQ_BARS_DAILY', false, 9, 'Optional paid challenger.'),
  ('bindings-1.0.0', 'EOD_HISTORY_JP', 'jquants', 'JQ_EQ_BARS_DAILY', false, 9, 'Optional paid challenger.'),
  ('bindings-1.0.0', 'EOD_CURRENT_US', 'eodhd', 'EODHD_US_EOD_BULK', false, 9, 'Optional paid challenger.'),
  ('bindings-1.0.0', 'EOD_HISTORY_US', 'eodhd', 'EODHD_US_EOD_BULK', false, 9, 'Optional paid challenger.'),
  ('bindings-1.0.0', 'CORPORATE_ACTIONS_US', 'eodhd', 'EODHD_SPLITS', false, 9, 'Optional paid challenger.')
on conflict (binding_version, role, provider_id) do nothing;

-- ---------------------------------------------------------------- the readers
create or replace function market.provider_for_role(
  p_role market.provider_role,
  p_binding_version text default 'bindings-1.0.0'
)
returns text
language sql
stable
set search_path = ''
as $$
  select provider_id
  from market.provider_role_bindings
  where role = p_role
    and binding_version = p_binding_version
    and enabled
    and effective_to is null
  order by priority
  limit 1;
$$;

comment on function market.provider_for_role(market.provider_role, text) is
  'The provider currently filling a role, or nothing when the role is unfilled. Callers ask for the role; swapping the provider behind it changes no caller.';

create or replace view market.unfilled_roles as
  select r.role
  from unnest(enum_range(null::market.provider_role)) as r(role)
  where market.provider_for_role(r.role) is null
  order by 1;

comment on view market.unfilled_roles is
  'Roles no enabled provider fills. This is the honest list of what the platform currently cannot do, and it is expected to be non-empty while a zero-cost source is still being found.';

create or replace view market.recurring_cost as
  select coalesce(sum(p.monthly_cost_jpy), 0) as monthly_cost_jpy,
         count(*) as enabled_paid_providers,
         coalesce(array_agg(distinct p.provider_id), '{}') as providers
  from market.providers p
  where p.cost_class = 'OPTIONAL_PAID'
    and exists (
      select 1 from market.provider_role_bindings b
      where b.provider_id = p.provider_id and b.enabled and b.effective_to is null
    );

comment on view market.recurring_cost is
  'What the platform currently costs to keep running, per month. The core target is zero: a non-zero figure here means an optional paid provider has been deliberately enabled.';

-- ----------------------------------------------------------------- privileges
revoke all on table market.providers, market.provider_role_bindings from public;
grant select on table market.providers, market.provider_role_bindings
  to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;
grant select on market.unfilled_roles, market.recurring_cost
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function market.provider_for_role(market.provider_role, text)
  to surge_worker_prod, surge_worker_research, surge_readonly;
