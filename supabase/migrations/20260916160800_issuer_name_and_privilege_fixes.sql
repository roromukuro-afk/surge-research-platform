-- Phase 1.1a follow-up: defects found reviewing the Phase 1.1a patch itself.
--
-- 1. The runtime worker could not actually run the loader. The signed-URL
--    loader is SECURITY INVOKER and calls extensions.http, but surge_worker_prod
--    had no USAGE on schema extensions, so the EXECUTE grant was inert and the
--    documented runtime path would have failed on the first production run.
-- 2. Every function in ref/pipeline/universe was executable by PUBLIC (the
--    Postgres default), including ref.apply_master_snapshot and the loader.
--    Table grants still contained the damage, but the default contradicts the
--    least-privilege boundary this phase is about.
-- 3. The registry name reached ref.issuers.legal_name but not normalized_name,
--    which was still the normalised *security* name, so issuer matching would
--    have used a product name.
-- 4. ref.issuer_names rows carried the security's source_record_id next to the
--    registry's source_id: provenance that does not describe the row.
-- 5. ref.listings_as_of called a set-returning function inside a lateral, which
--    re-scanned every ticker row for every listing.

-- ---------------------------------------------- 1. the loader must be runnable
grant usage on schema extensions to surge_worker_prod;

comment on schema extensions is
  'Extension objects. surge_worker_prod needs USAGE to run pipeline.load_master_snapshot_from_signed_url, which is SECURITY INVOKER.';

-- --------------------------------------------- 2. functions are not public
do $$
declare
  fn record;
begin
  for fn in
    select n.nspname as schema_name, p.oid::regprocedure as signature
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname in ('ref', 'pipeline', 'universe')
  loop
    execute format('revoke all on function %s from public', fn.signature);
  end loop;
end
$$;

-- re-grant exactly what each role needs
grant execute on function
  ref.deterministic_uuid(text),
  ref.identity_confidence_rank(text),
  ref.identity_confidence_label(integer),
  ref.listings_as_of(timestamptz),
  ref.listing_symbols_as_of(timestamptz),
  pipeline.assert_load_url_allowed(text),
  pipeline.snapshot_issuers(uuid),
  pipeline.snapshot_securities(uuid),
  pipeline.snapshot_listings(uuid),
  pipeline.snapshot_identifiers(uuid)
to surge_worker_prod, surge_worker_research, surge_readonly;

grant execute on function
  ref.apply_master_snapshot(uuid),
  universe.apply_snapshot_evaluations(uuid, text, date),
  universe.compute_coverage(uuid, text, date, jsonb, text)
to surge_worker_prod;

do $$
begin
  if exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'pipeline' and p.proname = 'load_master_snapshot_from_signed_url'
  ) then
    execute 'grant execute on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) to surge_worker_prod';
  end if;
end
$$;

-- ------------------------------------ 3./4. issuer name provenance and matching
alter table pipeline.master_snapshot
  add column if not exists issuer_normalized_name text;

comment on column pipeline.master_snapshot.issuer_normalized_name is
  'The issuer registry name, normalised by the worker. Falls back to the security''s normalised name only when no registry name exists.';

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
         -- matched on the issuer's own name when there is one
         coalesce(min(s.issuer_normalized_name), min(s.normalized_name)),
         case when min(s.market_code::text) = 'JP' then 'ja' else 'en' end,
         min(s.observed_at),
         min(s.available_at),
         min(s.source_id),
         -- the provider record id identifies a security, not this issuer name
         null::text
  from pipeline.master_snapshot s
  where s.run_id = p_run_id
  group by s.issuer_identity_key;
$$;

grant execute on function pipeline.snapshot_issuers(uuid)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- The confirm branch of the issuer-name SCD2 also refreshes the normalised form,
-- so an existing row picks the corrected value up without a rebuild.
create or replace function ref.refresh_issuer_name_matching(p_run_id uuid)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  update ref.issuer_names ins
  set normalized_name = i.normalized_name
  from pipeline.snapshot_issuers(p_run_id) i
  where ins.issuer_id = i.issuer_id
    and ins.name_type = i.name_type
    and ins.effective_to is null
    and ins.normalized_name is distinct from i.normalized_name;
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function ref.refresh_issuer_name_matching(uuid) is
  'Brings the normalised issuer name of existing open rows in line with the snapshot. Matching evidence only: it never changes a name or an identity.';

grant execute on function ref.refresh_issuer_name_matching(uuid) to surge_worker_prod;

-- The apply path itself keeps the issuer name matching current, so a normal run
-- repairs an existing row without a rebuild.
create or replace function ref.apply_master_snapshot_with_name_refresh(p_run_id uuid)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_result jsonb;
  v_refreshed int;
begin
  v_result := ref.apply_master_snapshot(p_run_id);
  v_refreshed := ref.refresh_issuer_name_matching(p_run_id);
  return v_result || jsonb_build_object('issuer_names_rematched', v_refreshed);
end;
$$;

comment on function ref.apply_master_snapshot_with_name_refresh(uuid) is
  'ref.apply_master_snapshot plus the issuer name matching refresh. This is what the runbook and the rebuild script call.';

grant execute on function ref.apply_master_snapshot_with_name_refresh(uuid) to surge_worker_prod;

-- --------------------------------------------- 5. as-of without the O(n^2) scan
create or replace function ref.listings_as_of(p_available_at timestamptz)
returns table (
  listing_id           uuid,
  security_id          uuid,
  exchange_id          text,
  symbol               text,
  market_segment_code  text,
  listing_status       ref.listing_status,
  effective_from       timestamptz,
  effective_to         timestamptz,
  symbol_effective_from timestamptz
)
language sql
stable
set search_path = ''
as $$
  select l.listing_id,
         l.security_id,
         l.exchange_id,
         sym.symbol,
         st.market_segment_code,
         st.listing_status,
         st.effective_from,
         st.effective_to,
         sym.effective_from
  from ref.listings l
  join ref.listing_states st
    on st.listing_id = l.listing_id
   and st.available_at <= p_available_at
   and (st.effective_to is null or st.effective_to > p_available_at)
  left join lateral (
    -- read the ticker history directly so the index on (listing_id, ...) is
    -- usable: going through ref.listing_symbols_as_of re-scanned the whole table
    -- once per listing.
    select ls.symbol, ls.effective_from
    from ref.listing_symbols ls
    where ls.listing_id = l.listing_id
      and ls.symbol_type = 'TICKER'
      and ls.available_at <= p_available_at
      and (ls.effective_to is null or ls.effective_to > p_available_at)
    order by ls.effective_from desc
    limit 1
  ) sym on true;
$$;

comment on function ref.listings_as_of(timestamptz) is
  'The listing master as it was known at a point in time: versioned attributes from ref.listing_states and the ticker that was current then from ref.listing_symbols. effective_from / effective_to describe the state row; symbol_effective_from describes the ticker row.';

grant execute on function ref.listings_as_of(timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- ------------------------------------------------------------- loader v4
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
        issuer_name, issuer_name_source, identity_version, issuer_normalized_name
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
             nullif(f[34], ''), nullif(f[35], ''), nullif(f[36], ''), nullif(f[37], '')
      from (
        select string_to_array(btrim(line, chr(13)), chr(31)) as f
        from regexp_split_to_table(v_response.content, chr(10)) as line
        where length(btrim(line, chr(13))) > 0
      ) parsed
      where array_length(f, 1) = 37
      on conflict (run_id, source_id, source_record_id) do nothing;

      get diagnostics v_rows = row_count;
      return v_rows;
    end;
    $fn$;
  $sql$;

  execute $sql$
    comment on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) is
      'Loads a 37 field unit separator delimited snapshot from a signed object storage URL. No credential is passed to or stored in the database.'
  $sql$;

  execute 'grant execute on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) to surge_worker_prod';
end
$migration$;
