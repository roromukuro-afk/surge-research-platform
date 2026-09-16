-- Phase 1.1b: the worker may load a snapshot, not make arbitrary HTTP requests.
--
-- 20260916160800 gave surge_worker_prod USAGE on schema extensions so that the
-- SECURITY INVOKER loader could call extensions.http. That also let the worker
-- call extensions.http / http_get / http_post directly, which walks straight
-- past pipeline.assert_load_url_allowed: the allowlist stopped being a control.
--
-- The loader becomes SECURITY DEFINER instead. The privilege to make an HTTP
-- request now belongs to the function, not to the role, and the only way to use
-- it is through the allowlist check inside it.

-- ------------------------------------------------- 1. the loader owns the right
do $migration$
begin
  if not exists (select 1 from pg_available_extensions where name = 'http') then
    raise notice 'http extension unavailable: nothing to harden here';
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
    security definer
    set search_path = ''
    as $fn$
    declare
      v_response extensions.http_response;
      v_rows integer;
    begin
      -- The caller cannot reach extensions.http on its own; this check is the
      -- only door, so it runs before anything else.
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
      'Loads a 37 field unit separator delimited snapshot from a signed object storage URL. SECURITY DEFINER: the right to make the request belongs to this function, which checks pipeline.assert_load_url_allowed first, not to the calling role.'
  $sql$;

  execute 'revoke all on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) from public';
  execute 'grant execute on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) to surge_worker_prod';

  -- ------------------------------------- 2. nobody else reaches the http client
  execute 'revoke all on schema extensions from surge_worker_prod, surge_worker_prod_app, surge_worker_research, surge_readonly';

  -- Best effort: on a managed platform these functions are owned by another
  -- role, which makes this REVOKE a silent no-op (see 20260916170700). Schema
  -- USAGE above is the control that actually holds, and
  -- pipeline.assert_http_boundary() checks it.
  declare
    fn record;
  begin
    for fn in
      select p.oid::regprocedure as signature
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'extensions' and p.proname like 'http%'
    loop
      execute format('revoke all on function %s from public', fn.signature);
      execute format(
        'revoke all on function %s from surge_worker_prod, surge_worker_prod_app, surge_worker_research, surge_readonly',
        fn.signature
      );
    end loop;
  end;
end
$migration$;

comment on function pipeline.assert_load_url_allowed(text) is
  'Rejects non-https URLs, hosts that are not registered, and URLs that are not signed. Called by the SECURITY DEFINER loader before any request is made; no runtime role can reach the HTTP client without it.';
