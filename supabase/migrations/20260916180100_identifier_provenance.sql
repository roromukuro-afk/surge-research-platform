-- Phase 1.1c: an identifier's provenance points at the source that gave it.
--
-- Every identifier and issuer name row carried the PRIMARY provider's source_id
-- and timestamps, so the CIK looked as though Nasdaq had supplied it and the
-- EDINET code as though JPX had. That is wrong about where the value came from,
-- and it dates the value to the wrong fetch.
--
-- Two timestamps, two jobs:
--   observed_at  - when the source that actually supplied the value was read
--   available_at - when the row could be known by the system at all, which for
--                  a row keyed on a resolved identity is the moment the LAST
--                  source the identity depends on arrived (audit item 7: a
--                  CIK-derived security_id must not be visible before the CIK
--                  lookup existed). Later is safe; earlier is not.

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
  with fetched as (
    select f.source_id,
           min(f.observed_at) as observed_at,
           max(f.available_at) as available_at
    from pipeline.source_fetches f
    where f.run_id = p_run_id
    group by f.source_id
  ),
  dependency as (
    select max(f.available_at) as available_at from pipeline.source_fetches f where f.run_id = p_run_id
  ),
  securities as (select * from pipeline.snapshot_securities(p_run_id))
  -- the exchange code / ticker comes from the market data provider
  select s.security_id,
         case when s.market_code = 'JP' then 'LOCAL_CODE' else 'TICKER' end,
         case when s.market_code = 'JP' then 'jpx' else 'nasdaq_trader' end,
         coalesce(s.symbol, s.local_code),
         coalesce(f.observed_at, s.observed_at),
         coalesce((select available_at from dependency), s.available_at),
         coalesce(f.source_id, s.source_id),
         s.source_record_id
  from securities s
  left join fetched f
    on f.source_id = case when s.market_code = 'JP' then 'jpx_listed_issues'
                          else 'nasdaq_trader_symbol_directory' end
  union all
  -- the CIK comes from the SEC ticker file, keyed there by ticker
  select s.security_id, 'CIK', 'sec', s.cik,
         coalesce(f.observed_at, s.observed_at),
         coalesce((select available_at from dependency), s.available_at),
         coalesce(f.source_id, 'sec_company_tickers'),
         coalesce(s.symbol, s.local_code)
  from securities s
  left join fetched f on f.source_id = 'sec_company_tickers'
  where s.cik is not null
  union all
  -- the EDINET code comes from the EDINET code list, keyed there by that code
  select s.security_id, 'EDINET', 'edinet', s.edinet_code,
         coalesce(f.observed_at, s.observed_at),
         coalesce((select available_at from dependency), s.available_at),
         coalesce(f.source_id, 'edinet_code_list'),
         s.edinet_code
  from securities s
  left join fetched f on f.source_id = 'edinet_code_list'
  where s.edinet_code is not null
  union all
  -- so does the Japanese corporate number
  select s.security_id, 'PROVIDER_ID', 'houjin_bango', s.corporate_number,
         coalesce(f.observed_at, s.observed_at),
         coalesce((select available_at from dependency), s.available_at),
         coalesce(f.source_id, 'edinet_code_list'),
         s.edinet_code
  from securities s
  left join fetched f on f.source_id = 'edinet_code_list'
  where s.corporate_number is not null;
$$;

comment on function pipeline.snapshot_identifiers(uuid) is
  'Identifiers with the provenance of the source that supplied each one. observed_at is that source''s read time; available_at is the run dependency maximum, so an identity-keyed row is never visible before the lookups that produced the identity.';

grant execute on function pipeline.snapshot_identifiers(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- --------------------------------------------------------- issuer name times
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
  with fetched as (
    select f.source_id, min(f.observed_at) as observed_at, max(f.available_at) as available_at
    from pipeline.source_fetches f
    where f.run_id = p_run_id
    group by f.source_id
  ),
  dependency as (
    select max(f.available_at) as available_at from pipeline.source_fetches f where f.run_id = p_run_id
  ),
  grouped as (
    select s.issuer_identity_key as identity_key,
           min(s.issuer_identity_source) as identity_source,
           ref.identity_confidence_label(min(ref.identity_confidence_rank(s.issuer_identity_confidence)))
             as identity_confidence,
           min(s.identity_version) as identity_version,
           min(s.country) as country,
           coalesce(min(s.issuer_name), min(s.name)) as legal_name,
           case when min(s.issuer_name) is not null then 'LEGAL' else 'ALIAS' end as name_type,
           coalesce(min(s.issuer_name_source), min(s.source_id)) as name_source,
           coalesce(min(s.issuer_normalized_name), min(s.normalized_name)) as normalized_name,
           case when min(s.market_code::text) = 'JP' then 'ja' else 'en' end as language,
           min(s.observed_at) as snapshot_observed_at,
           min(s.available_at) as snapshot_available_at
    from pipeline.master_snapshot s
    where s.run_id = p_run_id
    group by s.issuer_identity_key
  )
  select ref.deterministic_uuid('issuer|' || g.identity_key),
         g.identity_source,
         g.identity_key,
         g.identity_confidence,
         g.identity_version,
         g.country,
         g.legal_name,
         g.name_type,
         g.name_source,
         g.normalized_name,
         g.language,
         -- the registry that supplied the name, not the primary provider
         coalesce(f.observed_at, g.snapshot_observed_at),
         coalesce((select available_at from dependency), g.snapshot_available_at),
         g.name_source,
         null::text
  from grouped g
  left join fetched f on f.source_id = g.name_source;
$$;

comment on function pipeline.snapshot_issuers(uuid) is
  'One row per issuer identity, named by the registry when there is one. observed_at is the read time of the source that supplied the name; available_at is the run dependency maximum.';

grant execute on function pipeline.snapshot_issuers(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;
