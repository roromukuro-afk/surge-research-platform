-- Phase 1.1: identity that does not depend on tickers or names, and a real
-- bitemporal listing state.
--
-- Three defects from the Phase 1 audit are addressed here:
--   * security / listing identity was derived from (exchange, local_code), which
--     for US listings is the ticker, so a ticker change created a new security;
--   * issuer identity was derived from a normalised name, which both split one
--     issuer across several ids and merged different issuers into one;
--   * listing attributes were overwritten in place, so a later snapshot erased
--     what was known earlier.

-- --------------------------------------------------------------- identity keys
alter table ref.issuers
  add column if not exists identity_source text,
  add column if not exists identity_key text,
  add column if not exists identity_confidence text not null default 'PROVISIONAL';

alter table ref.securities
  add column if not exists identity_source text,
  add column if not exists identity_key text,
  add column if not exists identity_confidence text not null default 'PROVISIONAL';

alter table ref.listings
  add column if not exists listing_identity_key text,
  add column if not exists last_confirmed_at timestamptz;

comment on column ref.issuers.identity_key is
  'Registry identity (SEC CIK / EDINET code) when available, otherwise a name-derived PROVISIONAL key.';
comment on column ref.issuers.normalized_name is
  'Attribute and matching evidence only. Never an authoritative identity key.';
comment on column ref.securities.identity_key is
  'Stable instrument identity. Tickers are attributes recorded in ref.listing_symbols.';

create unique index if not exists issuers_identity_key_uq on ref.issuers (identity_key);
create unique index if not exists securities_identity_key_uq on ref.securities (identity_key);
create unique index if not exists listings_identity_key_uq on ref.listings (listing_identity_key);

-- ------------------------------------------------------- bitemporal state rows
create table if not exists ref.listing_states (
  listing_state_id     uuid primary key default gen_random_uuid(),
  listing_id           uuid not null references ref.listings (listing_id),
  market_segment_code  text,
  market_segment_name  text,
  listing_status       ref.listing_status not null,
  is_primary           boolean not null default true,
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  last_confirmed_at    timestamptz,
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text,
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now()
);

comment on table ref.listing_states is
  'Versioned listing attributes. This is the source of truth for as-of reconstruction; ref.listings only carries the materialised current state.';

create index if not exists listing_states_listing_idx on ref.listing_states (listing_id);
create unique index if not exists listing_states_open_uq
  on ref.listing_states (listing_id) where effective_to is null;

-- history tables gain a confirmation timestamp so an unchanged value can be
-- re-observed without inserting another row (SCD2).
alter table ref.listing_symbols add column if not exists last_confirmed_at timestamptz;
alter table ref.security_names add column if not exists last_confirmed_at timestamptz;
alter table ref.security_identifiers add column if not exists last_confirmed_at timestamptz;
alter table ref.listing_status_history add column if not exists last_confirmed_at timestamptz;

create unique index if not exists listing_symbols_open_uq
  on ref.listing_symbols (listing_id, symbol_type) where effective_to is null;
create unique index if not exists security_names_open_uq
  on ref.security_names (security_id, name_type) where effective_to is null;
create unique index if not exists security_identifiers_open_uq
  on ref.security_identifiers (security_id, id_type, id_namespace) where effective_to is null;

-- ------------------------------------------------------------ staging columns
alter table pipeline.master_snapshot
  add column if not exists edinet_code text,
  add column if not exists corporate_number text,
  add column if not exists issuer_identity_source text,
  add column if not exists issuer_identity_key text,
  add column if not exists issuer_identity_confidence text,
  add column if not exists security_identity_source text,
  add column if not exists security_identity_key text,
  add column if not exists security_identity_confidence text;

-- --------------------------------------------- evaluations: no NULL duplicates
alter table universe.evaluations alter column security_id set not null;
alter table universe.evaluations drop constraint if exists evaluations_run_id_universe_version_as_of_date_listing_id_key;
create unique index if not exists evaluations_subject_uq
  on universe.evaluations (run_id, universe_version, as_of_date, security_id);

comment on index universe.evaluations_subject_uq is
  'security_id is always present, so re-applying a run cannot duplicate evaluations for records whose listing could not be resolved.';

-- --------------------------------------------------- identity migration record
create table if not exists ref.identity_migration_map (
  map_id               uuid primary key default gen_random_uuid(),
  migration_label      text not null,
  market_code          ref.market_code not null,
  exchange_id          text,
  local_code           text not null,
  symbol               text,
  old_security_id      uuid,
  old_listing_id       uuid,
  old_issuer_id        uuid,
  new_security_id      uuid,
  new_listing_id       uuid,
  new_issuer_id        uuid,
  note                 text,
  created_at           timestamptz not null default now()
);

comment on table ref.identity_migration_map is
  'Old to new identifier mapping for documented rebuilds, so a rebuild is auditable rather than silent.';

-- ------------------------------------------------------------- new source row
insert into pipeline.sources (source_id, name, market_code, source_kind, base_url, terms_url, terms_checked_at, full_text_allowed, notes) values
  ('edinet_code_list', 'EDINET code list (EdinetcodeDlInfo)', 'JP', 'REGULATOR',
   'https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip',
   'https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html', '2026-09-16', true,
   'Maps the exchange securities code to an EDINET code and the Japanese corporate number.')
on conflict (source_id) do nothing;

-- ------------------------------------------------------------ as-of from state
create or replace function ref.listings_as_of(p_available_at timestamptz)
returns table (
  listing_id uuid,
  security_id uuid,
  exchange_id text,
  local_code text,
  market_segment_code text,
  listing_status ref.listing_status,
  effective_from timestamptz,
  effective_to timestamptz
)
language sql
stable
set search_path = ''
as $$
  select l.listing_id,
         l.security_id,
         l.exchange_id,
         l.local_code,
         st.market_segment_code,
         st.listing_status,
         st.effective_from,
         st.effective_to
  from ref.listing_states st
  join ref.listings l on l.listing_id = st.listing_id
  where st.available_at <= p_available_at
    and st.effective_from <= p_available_at
    and (st.effective_to is null or st.effective_to > p_available_at);
$$;

comment on function ref.listings_as_of(timestamptz) is
  'Reconstructs listing state as it was known at p_available_at from versioned state rows. A later snapshot closes a state row but never erases it.';

grant execute on function ref.listings_as_of(timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- new tables inherit the same access model
grant select, insert, update on ref.listing_states, ref.identity_migration_map to surge_worker_prod;
grant select on ref.listing_states, ref.identity_migration_map to surge_worker_research, surge_readonly;

do $$
declare
  t record;
begin
  for t in select unnest(array['listing_states', 'identity_migration_map']) as tablename loop
    execute format('alter table ref.%I enable row level security', t.tablename);
    if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = t.tablename and policyname = 'surge_worker_prod_all') then
      execute format('create policy surge_worker_prod_all on ref.%I as permissive for all to surge_worker_prod using (true) with check (true)', t.tablename);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = t.tablename and policyname = 'surge_worker_research_read') then
      execute format('create policy surge_worker_research_read on ref.%I as permissive for select to surge_worker_research using (true)', t.tablename);
    end if;
    if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = t.tablename and policyname = 'surge_readonly_read') then
      execute format('create policy surge_readonly_read on ref.%I as permissive for select to surge_readonly using (true)', t.tablename);
    end if;
  end loop;
end
$$;
