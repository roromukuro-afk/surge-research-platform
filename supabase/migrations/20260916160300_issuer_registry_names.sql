-- Phase 1.1a: an issuer is named by its registry, not by one of its products.
--
-- ref.issuers.legal_name was min(security display name) over the issuer's
-- securities, so "Bank of Montreal" was stored as one of its 45 ETN product
-- names and Apple as "Apple Inc. - Common Stock". The registry name was already
-- being fetched and thrown away: the SEC company_tickers file carries the EDGAR
-- registrant name next to the CIK, and the EDINET code list carries 提出者名.
--
-- Issuer names now travel through the snapshot and are kept as SCD2 history in
-- ref.issuer_names (LEGAL when it comes from a registry, ALIAS when all we have
-- is the provider's security wording). The security's own name is unchanged and
-- still lives in ref.security_names.

-- ------------------------------------------------------------ staging columns
alter table pipeline.master_snapshot
  add column if not exists issuer_name text,
  add column if not exists issuer_name_source text,
  add column if not exists identity_version text;

comment on column pipeline.master_snapshot.issuer_name is
  'The issuing entity''s own registry name (SEC registrant / EDINET 提出者名). Never the security display name.';

-- -------------------------------------------------- identity ruleset version
alter table ref.issuers add column if not exists identity_version text;
alter table ref.securities add column if not exists identity_version text;

comment on column ref.securities.identity_version is
  'Version of the identity ruleset that assigned identity_key. A later promotion (a real security-level registry identifier) is a recorded migration, not a silent re-key, and Phase 2 raw market data must carry this alongside the provider''s own keys.';

-- ------------------------------------------------------------- issuer names
create table if not exists ref.issuer_names (
  issuer_name_id    uuid primary key default gen_random_uuid(),
  issuer_id         uuid not null references ref.issuers (issuer_id),
  name              text not null,
  normalized_name   text,
  name_type         text not null,
  language          text,
  effective_from    timestamptz not null,
  effective_to      timestamptz,
  observed_at       timestamptz not null,
  available_at      timestamptz not null,
  last_confirmed_at timestamptz,
  source_id         text not null references pipeline.sources (source_id),
  source_record_id  text,
  ingestion_run_id  uuid not null references pipeline.runs (run_id),
  created_at        timestamptz not null default now(),
  constraint issuer_names_type_ck check (name_type in ('LEGAL', 'FORMER', 'ALIAS'))
);

comment on table ref.issuer_names is
  'Issuer names over time. LEGAL comes from a registry (SEC registrant name, EDINET 提出者名); ALIAS is matching evidence only - it never decides that two issuers are the same one.';

create index if not exists issuer_names_issuer_idx on ref.issuer_names (issuer_id);
create index if not exists issuer_names_normalized_idx on ref.issuer_names (normalized_name);
create unique index if not exists issuer_names_open_uq
  on ref.issuer_names (issuer_id, name_type) where effective_to is null;

-- Default privileges no longer grant write (20260916160000), so say it here.
grant select, insert, update on ref.issuer_names to surge_worker_prod;
grant select on ref.issuer_names to surge_worker_research, surge_readonly;

alter table ref.issuer_names enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = 'issuer_names' and policyname = 'surge_worker_prod_all') then
    create policy surge_worker_prod_all on ref.issuer_names as permissive for all to surge_worker_prod using (true) with check (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = 'issuer_names' and policyname = 'surge_worker_research_read') then
    create policy surge_worker_research_read on ref.issuer_names as permissive for select to surge_worker_research using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'ref' and tablename = 'issuer_names' and policyname = 'surge_readonly_read') then
    create policy surge_readonly_read on ref.issuer_names as permissive for select to surge_readonly using (true);
  end if;
end
$$;

-- ---------------------------------------------------------- snapshot helpers
create or replace function pipeline.snapshot_issuers(p_run_id uuid)
returns table (
  issuer_id           uuid,
  identity_source     text,
  identity_key        text,
  identity_confidence text,
  identity_version    text,
  country             text,
  legal_name          text,
  name_type           text,
  name_source         text,
  normalized_name     text,
  language            text,
  observed_at         timestamptz,
  available_at        timestamptz,
  source_id           text,
  source_record_id    text
)
language sql
stable
set search_path = ''
as $$
  select ref.deterministic_uuid('issuer|' || s.issuer_identity_key),
         min(s.issuer_identity_source),
         s.issuer_identity_key,
         -- the least confident member decides, deliberately, not alphabetically
         ref.identity_confidence_label(min(ref.identity_confidence_rank(s.issuer_identity_confidence))),
         min(s.identity_version),
         min(s.country),
         coalesce(min(s.issuer_name), min(s.name)),
         case when min(s.issuer_name) is not null then 'LEGAL' else 'ALIAS' end,
         coalesce(min(s.issuer_name_source), min(s.source_id)),
         min(s.normalized_name),
         case when min(s.market_code::text) = 'JP' then 'ja' else 'en' end,
         min(s.observed_at),
         min(s.available_at),
         min(s.source_id),
         min(s.source_record_id)
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  group by s.issuer_identity_key;
$$;

comment on function pipeline.snapshot_issuers(uuid) is
  'One row per issuer identity in a snapshot, with the registry name when the provider gave one and the security display name marked as an ALIAS when it did not.';

drop function if exists pipeline.snapshot_securities(uuid);

create function pipeline.snapshot_securities(p_run_id uuid)
returns table (
  security_id uuid,
  issuer_id uuid,
  market_code ref.market_code,
  security_type ref.security_type,
  security_type_evidence jsonb,
  is_adr boolean,
  is_spac_pre_merger boolean,
  currency text,
  identity_source text,
  identity_key text,
  identity_confidence text,
  identity_version text,
  name text,
  normalized_name text,
  symbol text,
  local_code text,
  cik text,
  edinet_code text,
  corporate_number text,
  observed_at timestamptz,
  available_at timestamptz,
  source_id text,
  source_record_id text
)
language sql
stable
set search_path = ''
as $$
  select distinct on (s.security_identity_key)
         ref.deterministic_uuid('security|' || s.security_identity_key),
         ref.deterministic_uuid('issuer|' || s.issuer_identity_key),
         s.market_code, s.security_type, s.type_evidence, s.is_adr, s.is_spac_pre_merger, s.currency,
         s.security_identity_source, s.security_identity_key, s.security_identity_confidence,
         s.identity_version,
         s.name, s.normalized_name, s.symbol, s.local_code, s.cik, s.edinet_code, s.corporate_number,
         s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  order by s.security_identity_key, s.source_record_id;
$$;

grant execute on function pipeline.snapshot_securities(uuid), pipeline.snapshot_issuers(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- ------------------------------------------------------------- apply v3
create or replace function ref.apply_master_snapshot(p_run_id uuid)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_issuers int;
  v_securities int;
  v_listings int;
  v_states_closed int;
  v_states_opened int;
  v_symbols_closed int;
  v_symbols_opened int;
  v_names_closed int;
  v_names_opened int;
  v_identifiers_closed int;
  v_identifiers_opened int;
  v_issuer_names_closed int;
  v_issuer_names_opened int;
begin
  insert into ref.issuers (
    issuer_id, country, legal_name, normalized_name, name_source,
    identity_source, identity_key, identity_confidence, identity_version
  )
  select i.issuer_id, i.country, i.legal_name, i.normalized_name, i.name_source,
         i.identity_source, i.identity_key, i.identity_confidence, i.identity_version
  from pipeline.snapshot_issuers(p_run_id) i
  on conflict (issuer_id) do update set
    legal_name = excluded.legal_name,
    normalized_name = excluded.normalized_name,
    name_source = excluded.name_source,
    identity_source = excluded.identity_source,
    identity_confidence = excluded.identity_confidence,
    identity_version = excluded.identity_version;
  get diagnostics v_issuers = row_count;

  -- issuer names as SCD2: a changed name closes the open row, an unchanged one
  -- only moves the confirmation time.
  update ref.issuer_names ins
  set effective_to = i.observed_at
  from pipeline.snapshot_issuers(p_run_id) i
  where ins.issuer_id = i.issuer_id
    and ins.name_type = i.name_type
    and ins.effective_to is null
    and ins.name is distinct from i.legal_name;
  get diagnostics v_issuer_names_closed = row_count;

  update ref.issuer_names ins
  set last_confirmed_at = greatest(coalesce(ins.last_confirmed_at, ins.observed_at), i.observed_at)
  from pipeline.snapshot_issuers(p_run_id) i
  where ins.issuer_id = i.issuer_id
    and ins.name_type = i.name_type
    and ins.effective_to is null
    and ins.name = i.legal_name;

  insert into ref.issuer_names (
    issuer_id, name, normalized_name, name_type, language, effective_from,
    observed_at, available_at, last_confirmed_at, source_id, source_record_id, ingestion_run_id
  )
  select i.issuer_id, i.legal_name, i.normalized_name, i.name_type, i.language, i.observed_at,
         i.observed_at, i.available_at, i.observed_at, i.name_source, i.source_record_id, p_run_id
  from pipeline.snapshot_issuers(p_run_id) i
  where not exists (
    select 1 from ref.issuer_names ins
    where ins.issuer_id = i.issuer_id and ins.name_type = i.name_type and ins.effective_to is null
  );
  get diagnostics v_issuer_names_opened = row_count;

  insert into ref.securities (
    security_id, issuer_id, market_code, security_type, security_type_source,
    security_type_evidence, is_adr, is_spac_pre_merger, currency,
    identity_source, identity_key, identity_confidence, identity_version,
    first_seen_at, last_seen_at
  )
  select s.security_id, s.issuer_id, s.market_code, s.security_type, s.source_id,
         s.security_type_evidence, s.is_adr, s.is_spac_pre_merger, s.currency,
         s.identity_source, s.identity_key, s.identity_confidence, s.identity_version,
         s.observed_at, s.observed_at
  from pipeline.snapshot_securities(p_run_id) s
  on conflict (security_id) do update set
    issuer_id = excluded.issuer_id,
    security_type = excluded.security_type,
    security_type_evidence = excluded.security_type_evidence,
    is_adr = excluded.is_adr,
    is_spac_pre_merger = excluded.is_spac_pre_merger,
    identity_source = excluded.identity_source,
    identity_confidence = excluded.identity_confidence,
    identity_version = excluded.identity_version,
    last_seen_at = greatest(ref.securities.last_seen_at, excluded.last_seen_at);
  get diagnostics v_securities = row_count;

  insert into ref.listings (
    listing_id, security_id, exchange_id, local_code, listing_identity_key,
    market_segment_code, market_segment_name, is_primary, listing_status,
    effective_from, observed_at, available_at, last_confirmed_at,
    source_id, source_record_id, ingestion_run_id
  )
  select l.listing_id, l.security_id, l.exchange_id, l.local_code, l.listing_identity_key,
         l.market_segment_code, l.market_segment_name, true, l.listing_status,
         l.observed_at, l.observed_at, l.available_at, l.observed_at,
         l.source_id, l.source_record_id, p_run_id
  from pipeline.snapshot_listings(p_run_id) l
  on conflict (listing_id) do update set
    local_code = excluded.local_code,
    market_segment_code = excluded.market_segment_code,
    market_segment_name = excluded.market_segment_name,
    listing_status = excluded.listing_status,
    observed_at = excluded.observed_at,
    available_at = excluded.available_at,
    last_confirmed_at = excluded.last_confirmed_at,
    source_record_id = excluded.source_record_id;
  get diagnostics v_listings = row_count;

  update ref.listing_states st
  set effective_to = l.observed_at
  from pipeline.snapshot_listings(p_run_id) l
  where st.listing_id = l.listing_id
    and st.effective_to is null
    and (st.market_segment_code is distinct from l.market_segment_code
      or st.market_segment_name is distinct from l.market_segment_name
      or st.listing_status is distinct from l.listing_status);
  get diagnostics v_states_closed = row_count;

  update ref.listing_states st
  set last_confirmed_at = greatest(coalesce(st.last_confirmed_at, st.observed_at), l.observed_at)
  from pipeline.snapshot_listings(p_run_id) l
  where st.listing_id = l.listing_id
    and st.effective_to is null;

  insert into ref.listing_states (
    listing_id, market_segment_code, market_segment_name, listing_status, is_primary,
    effective_from, observed_at, available_at, last_confirmed_at,
    source_id, source_record_id, ingestion_run_id
  )
  select l.listing_id, l.market_segment_code, l.market_segment_name, l.listing_status, true,
         l.observed_at, l.observed_at, l.available_at, l.observed_at,
         l.source_id, l.source_record_id, p_run_id
  from pipeline.snapshot_listings(p_run_id) l
  where not exists (
    select 1 from ref.listing_states st
    where st.listing_id = l.listing_id and st.effective_to is null
  );
  get diagnostics v_states_opened = row_count;

  update ref.listing_symbols ls
  set effective_to = l.observed_at
  from pipeline.snapshot_listings(p_run_id) l
  where ls.listing_id = l.listing_id
    and ls.symbol_type = 'TICKER'
    and ls.effective_to is null
    and ls.symbol is distinct from l.symbol;
  get diagnostics v_symbols_closed = row_count;

  update ref.listing_symbols ls
  set last_confirmed_at = greatest(coalesce(ls.last_confirmed_at, ls.observed_at), l.observed_at)
  from pipeline.snapshot_listings(p_run_id) l
  where ls.listing_id = l.listing_id
    and ls.symbol_type = 'TICKER'
    and ls.effective_to is null
    and ls.symbol = l.symbol;

  insert into ref.listing_symbols (
    listing_id, symbol, symbol_type, effective_from, observed_at, available_at,
    last_confirmed_at, source_id, source_record_id, ingestion_run_id
  )
  select l.listing_id, l.symbol, 'TICKER', l.observed_at, l.observed_at, l.available_at,
         l.observed_at, l.source_id, l.source_record_id, p_run_id
  from pipeline.snapshot_listings(p_run_id) l
  where not exists (
    select 1 from ref.listing_symbols ls
    where ls.listing_id = l.listing_id and ls.symbol_type = 'TICKER' and ls.effective_to is null
  );
  get diagnostics v_symbols_opened = row_count;

  update ref.security_names sn
  set effective_to = s.observed_at
  from pipeline.snapshot_securities(p_run_id) s
  where sn.security_id = s.security_id
    and sn.name_type = 'LEGAL'
    and sn.effective_to is null
    and sn.name is distinct from s.name;
  get diagnostics v_names_closed = row_count;

  update ref.security_names sn
  set last_confirmed_at = greatest(coalesce(sn.last_confirmed_at, sn.observed_at), s.observed_at)
  from pipeline.snapshot_securities(p_run_id) s
  where sn.security_id = s.security_id
    and sn.name_type = 'LEGAL'
    and sn.effective_to is null
    and sn.name = s.name;

  insert into ref.security_names (
    security_id, name, normalized_name, name_type, language, effective_from,
    observed_at, available_at, last_confirmed_at, source_id, source_record_id, ingestion_run_id
  )
  select s.security_id, s.name, s.normalized_name, 'LEGAL',
         case when s.market_code = 'JP' then 'ja' else 'en' end,
         s.observed_at, s.observed_at, s.available_at, s.observed_at,
         s.source_id, s.source_record_id, p_run_id
  from pipeline.snapshot_securities(p_run_id) s
  where not exists (
    select 1 from ref.security_names sn
    where sn.security_id = s.security_id and sn.name_type = 'LEGAL' and sn.effective_to is null
  );
  get diagnostics v_names_opened = row_count;

  update ref.security_identifiers si
  set effective_to = i.observed_at
  from pipeline.snapshot_identifiers(p_run_id) i
  where si.security_id = i.security_id
    and si.id_type = i.id_type
    and si.id_namespace is not distinct from i.id_namespace
    and si.effective_to is null
    and si.id_value is distinct from i.id_value;
  get diagnostics v_identifiers_closed = row_count;

  update ref.security_identifiers si
  set last_confirmed_at = greatest(coalesce(si.last_confirmed_at, si.observed_at), i.observed_at)
  from pipeline.snapshot_identifiers(p_run_id) i
  where si.security_id = i.security_id
    and si.id_type = i.id_type
    and si.id_namespace is not distinct from i.id_namespace
    and si.effective_to is null
    and si.id_value = i.id_value;

  insert into ref.security_identifiers (
    security_id, id_type, id_value, id_namespace, effective_from,
    observed_at, available_at, last_confirmed_at, source_id, source_record_id, ingestion_run_id
  )
  select i.security_id, i.id_type, i.id_value, i.id_namespace, i.observed_at,
         i.observed_at, i.available_at, i.observed_at, i.source_id, i.source_record_id, p_run_id
  from pipeline.snapshot_identifiers(p_run_id) i
  where not exists (
    select 1 from ref.security_identifiers si
    where si.security_id = i.security_id
      and si.id_type = i.id_type
      and si.id_namespace is not distinct from i.id_namespace
      and si.effective_to is null
  );
  get diagnostics v_identifiers_opened = row_count;

  return jsonb_build_object(
    'issuers_upserted', v_issuers,
    'issuer_names_closed', v_issuer_names_closed,
    'issuer_names_opened', v_issuer_names_opened,
    'securities_upserted', v_securities,
    'listings_upserted', v_listings,
    'listing_states_closed', v_states_closed,
    'listing_states_opened', v_states_opened,
    'symbols_closed', v_symbols_closed,
    'symbols_opened', v_symbols_opened,
    'names_closed', v_names_closed,
    'names_opened', v_names_opened,
    'identifiers_closed', v_identifiers_closed,
    'identifiers_opened', v_identifiers_opened
  );
end;
$$;

comment on function ref.apply_master_snapshot(uuid) is
  'Materialises a snapshot using stable identity keys and SCD2 history for listing state, tickers, security names, identifiers and issuer names. Issuer legal names come from the registry when the provider supplied one.';

grant execute on function ref.apply_master_snapshot(uuid) to surge_worker_prod;

-- ------------------------------------------------------------- loader v3
-- Three fields were appended to the snapshot, so the positional loader has to
-- learn them. Rows of the wrong width are dropped silently by design, which is
-- exactly why the width is asserted in the worker and in the tests.
do $migration$
begin
  if not exists (select 1 from pg_available_extensions where name = 'http') then
    raise notice 'http extension unavailable: pipeline.load_master_snapshot_from_signed_url is not installed here';
    return;
  end if;

  create schema if not exists extensions;
  execute 'create extension if not exists http with schema extensions';

  execute $sql$
    create or replace function pipeline.load_master_snapshot_from_signed_url(
      p_run_id uuid,
      p_url text,
      p_timeout_seconds integer default 120
    )
    returns integer
    language plpgsql
    set search_path = ''
    as $fn$
    declare
      v_response extensions.http_response;
      v_rows integer;
    begin
      perform pipeline.assert_load_url_allowed(p_url);
      perform extensions.http_set_curlopt('CURLOPT_TIMEOUT', p_timeout_seconds::text);

      -- No headers at all: the signed URL is the only credential.
      select *
      into v_response
      from extensions.http(('GET', p_url, array[]::extensions.http_header[], null, null)::extensions.http_request);

      if v_response.status <> 200 then
        raise exception 'snapshot fetch failed with status %', v_response.status;
      end if;

      insert into pipeline.master_snapshot (
        run_id, source_id, source_record_id, market_code, exchange_id, local_code, symbol,
        name, normalized_name, security_type, market_segment_code, market_segment_name,
        currency, country, is_adr, is_test_issue, is_spac_pre_merger, listing_status, cik,
        edinet_code, corporate_number, issuer_identity_source, issuer_identity_key,
        issuer_identity_confidence, security_identity_source, security_identity_key,
        security_identity_confidence, type_evidence, decision, reason_code, decision_detail,
        observed_at, available_at, source_data_version,
        issuer_name, issuer_name_source, identity_version
      )
      select p_run_id,
             f[1], f[2], f[3]::ref.market_code, nullif(f[4], ''), f[5], nullif(f[6], ''),
             f[7], f[8], f[9]::ref.security_type, nullif(f[10], ''), nullif(f[11], ''),
             f[12], f[13], f[14]::boolean, f[15]::boolean, nullif(f[16], '')::boolean,
             f[17]::ref.listing_status, nullif(f[18], ''), nullif(f[19], ''), nullif(f[20], ''),
             f[21], f[22], f[23], f[24], f[25], f[26],
             coalesce(nullif(f[27], ''), '{}')::jsonb,
             f[28]::universe.decision, f[29],
             coalesce(nullif(f[30], ''), '{}')::jsonb,
             f[31]::timestamptz, f[32]::timestamptz, f[33],
             nullif(f[34], ''), nullif(f[35], ''), nullif(f[36], '')
      from (
        select string_to_array(btrim(line, chr(13)), chr(31)) as f
        from regexp_split_to_table(v_response.content, chr(10)) as line
        where length(btrim(line, chr(13))) > 0
      ) parsed
      where array_length(f, 1) = 36
      on conflict (run_id, source_id, source_record_id) do nothing;

      get diagnostics v_rows = row_count;
      return v_rows;
    end;
    $fn$;
  $sql$;

  execute $sql$
    comment on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) is
      'Loads a 36 field unit separator delimited snapshot from a signed object storage URL. No credential is passed to or stored in the database.'
  $sql$;

  execute 'grant execute on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) to surge_worker_prod';
end
$migration$;
