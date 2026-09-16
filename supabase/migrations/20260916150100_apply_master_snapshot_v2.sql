-- Phase 1.1: snapshot materialisation with stable identity and SCD2 history.
--
-- Re-running an unchanged snapshot must not grow history. A changed value closes
-- the open row and opens a new one; an unchanged value only moves
-- last_confirmed_at.

-- Helper views over the staging rows of one run, deduplicated by identity.
create or replace function pipeline.snapshot_securities(p_run_id uuid)
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
         s.name, s.normalized_name, s.symbol, s.local_code, s.cik, s.edinet_code, s.corporate_number,
         s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  order by s.security_identity_key, s.source_record_id;
$$;

create or replace function pipeline.snapshot_listings(p_run_id uuid)
returns table (
  listing_id uuid,
  security_id uuid,
  exchange_id text,
  local_code text,
  listing_identity_key text,
  symbol text,
  market_segment_code text,
  market_segment_name text,
  listing_status ref.listing_status,
  observed_at timestamptz,
  available_at timestamptz,
  source_id text,
  source_record_id text
)
language sql
stable
set search_path = ''
as $$
  select distinct on (s.exchange_id, s.security_identity_key)
         ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.security_identity_key),
         ref.deterministic_uuid('security|' || s.security_identity_key),
         s.exchange_id, s.local_code,
         s.exchange_id || '|' || s.security_identity_key,
         coalesce(s.symbol, s.local_code),
         s.market_segment_code, s.market_segment_name, s.listing_status,
         s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
    and s.exchange_id is not null
  order by s.exchange_id, s.security_identity_key, s.source_record_id;
$$;

create or replace function pipeline.snapshot_identifiers(p_run_id uuid)
returns table (
  security_id uuid,
  id_type text,
  id_namespace text,
  id_value text,
  observed_at timestamptz,
  available_at timestamptz,
  source_id text,
  source_record_id text
)
language sql
stable
set search_path = ''
as $$
  select s.security_id,
         case when s.market_code = 'JP' then 'LOCAL_CODE' else 'TICKER' end,
         case when s.market_code = 'JP' then 'jpx' else 'nasdaq_trader' end,
         coalesce(s.symbol, s.local_code),
         s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.snapshot_securities(p_run_id) s
  union all
  select s.security_id, 'CIK', 'sec', s.cik, s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.snapshot_securities(p_run_id) s where s.cik is not null
  union all
  select s.security_id, 'EDINET', 'edinet', s.edinet_code, s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.snapshot_securities(p_run_id) s where s.edinet_code is not null
  union all
  select s.security_id, 'PROVIDER_ID', 'houjin_bango', s.corporate_number, s.observed_at, s.available_at, s.source_id, s.source_record_id
  from pipeline.snapshot_securities(p_run_id) s where s.corporate_number is not null;
$$;

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
begin
  -- ------------------------------------------------------------------ issuers
  insert into ref.issuers (
    issuer_id, country, legal_name, normalized_name, name_source,
    identity_source, identity_key, identity_confidence
  )
  select ref.deterministic_uuid('issuer|' || s.issuer_identity_key),
         min(s.country), min(s.name), min(s.normalized_name), min(s.source_id),
         min(s.issuer_identity_source), s.issuer_identity_key, min(s.issuer_identity_confidence)
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  group by s.issuer_identity_key
  on conflict (issuer_id) do update set
    legal_name = excluded.legal_name,
    normalized_name = excluded.normalized_name,
    identity_source = excluded.identity_source,
    identity_confidence = excluded.identity_confidence;
  get diagnostics v_issuers = row_count;

  -- --------------------------------------------------------------- securities
  insert into ref.securities (
    security_id, issuer_id, market_code, security_type, security_type_source,
    security_type_evidence, is_adr, is_spac_pre_merger, currency,
    identity_source, identity_key, identity_confidence, first_seen_at, last_seen_at
  )
  select s.security_id, s.issuer_id, s.market_code, s.security_type, s.source_id,
         s.security_type_evidence, s.is_adr, s.is_spac_pre_merger, s.currency,
         s.identity_source, s.identity_key, s.identity_confidence, s.observed_at, s.observed_at
  from pipeline.snapshot_securities(p_run_id) s
  on conflict (security_id) do update set
    issuer_id = excluded.issuer_id,
    security_type = excluded.security_type,
    security_type_evidence = excluded.security_type_evidence,
    is_adr = excluded.is_adr,
    is_spac_pre_merger = excluded.is_spac_pre_merger,
    identity_source = excluded.identity_source,
    identity_confidence = excluded.identity_confidence,
    last_seen_at = greatest(ref.securities.last_seen_at, excluded.last_seen_at);
  get diagnostics v_securities = row_count;

  -- ----------------------------------------------------------------- listings
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

  -- ------------------------------------------------------ listing state (SCD2)
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

  -- ---------------------------------------------------- ticker history (SCD2)
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

  -- ------------------------------------------------------ name history (SCD2)
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

  -- ------------------------------------------------ identifier history (SCD2)
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
  'Materialises a snapshot using stable identity keys and SCD2 history: unchanged values are confirmed, changed values close the open row and open a new one.';

-- evaluations now key on security_id, which is always present
create or replace function universe.apply_snapshot_evaluations(
  p_run_id uuid,
  p_universe_version text,
  p_as_of date
)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  insert into universe.evaluations (
    run_id, universe_version, as_of_date, market_code, security_id, listing_id,
    decision, reason_code, decision_detail, evaluated_at, source_data_version
  )
  select distinct on (s.security_identity_key)
         p_run_id, p_universe_version, p_as_of, s.market_code,
         ref.deterministic_uuid('security|' || s.security_identity_key),
         case when s.exchange_id is null then null
              else ref.deterministic_uuid('listing|' || s.exchange_id || '|' || s.security_identity_key) end,
         s.decision, s.reason_code, s.decision_detail, now(), s.source_data_version
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  order by s.security_identity_key, s.source_record_id
  on conflict (run_id, universe_version, as_of_date, security_id) do nothing;
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

grant execute on function ref.apply_master_snapshot(uuid) to surge_worker_prod;
grant execute on function universe.apply_snapshot_evaluations(uuid, text, date) to surge_worker_prod;
grant execute on function pipeline.snapshot_securities(uuid) to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function pipeline.snapshot_listings(uuid) to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function pipeline.snapshot_identifiers(uuid) to surge_worker_prod, surge_worker_research, surge_readonly;
