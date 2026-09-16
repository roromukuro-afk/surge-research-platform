-- Phase 1: bulk load a snapshot straight from object storage.
--
-- Bulk data must not travel through the tooling that orchestrates the run: the
-- worker writes a delimited file to object storage and the database reads it
-- directly. The same path carries OHLCV and other bulk datasets in later phases.
--
-- The loader needs the `http` extension. Managed Postgres (Supabase) ships it;
-- a plain postgres image in CI does not, so installation is conditional and the
-- rest of the schema stays usable without it.

do $migration$
begin
  if not exists (select 1 from pg_available_extensions where name = 'http') then
    raise notice 'http extension unavailable: pipeline.load_master_snapshot_from_url is not installed here';
    return;
  end if;

  create schema if not exists extensions;
  execute 'create extension if not exists http with schema extensions';

  execute $sql$
    create or replace function pipeline.load_master_snapshot_from_url(
      p_run_id uuid,
      p_url text,
      p_api_key text default null,
      p_timeout_seconds integer default 120
    )
    returns integer
    language plpgsql
    as $fn$
    declare
      v_headers extensions.http_header[] := array[]::extensions.http_header[];
      v_response extensions.http_response;
      v_rows integer;
    begin
      perform extensions.http_set_curlopt('CURLOPT_TIMEOUT', p_timeout_seconds::text);

      if p_api_key is not null then
        v_headers := array[
          extensions.http_header('apikey', p_api_key),
          extensions.http_header('Authorization', 'Bearer ' || p_api_key)
        ];
      end if;

      select *
      into v_response
      from extensions.http(('GET', p_url, v_headers, null, null)::extensions.http_request);

      if v_response.status <> 200 then
        raise exception 'snapshot fetch failed: % returned %', p_url, v_response.status;
      end if;

      -- Unit separator delimited: the delimiter cannot appear in issuer names or
      -- security names, so no quoting rules are needed.
      insert into pipeline.master_snapshot (
        run_id, source_id, source_record_id, market_code, exchange_id, local_code, symbol,
        name, normalized_name, security_type, market_segment_code, market_segment_name,
        currency, country, is_adr, is_test_issue, is_spac_pre_merger, listing_status, cik,
        type_evidence, decision, reason_code, decision_detail, observed_at, available_at,
        source_data_version
      )
      select p_run_id,
             f[1],
             f[2],
             f[3]::ref.market_code,
             nullif(f[4], ''),
             f[5],
             nullif(f[6], ''),
             f[7],
             f[8],
             f[9]::ref.security_type,
             nullif(f[10], ''),
             nullif(f[11], ''),
             f[12],
             f[13],
             f[14]::boolean,
             f[15]::boolean,
             nullif(f[16], '')::boolean,
             f[17]::ref.listing_status,
             nullif(f[18], ''),
             coalesce(nullif(f[19], ''), '{}')::jsonb,
             f[20]::universe.decision,
             f[21],
             coalesce(nullif(f[22], ''), '{}')::jsonb,
             f[23]::timestamptz,
             f[24]::timestamptz,
             f[25]
      from (
        select string_to_array(btrim(line, chr(13)), chr(31)) as f
        from regexp_split_to_table(v_response.content, chr(10)) as line
        where length(btrim(line, chr(13))) > 0
      ) parsed
      where array_length(f, 1) = 25
      on conflict (run_id, source_id, source_record_id) do nothing;

      get diagnostics v_rows = row_count;
      return v_rows;
    end;
    $fn$;
  $sql$;

  execute $sql$
    comment on function pipeline.load_master_snapshot_from_url(uuid, text, text, integer) is
      'Loads a unit-separator delimited snapshot from object storage into staging. The URL and any key are supplied per call and never stored.'
  $sql$;

  execute 'grant execute on function pipeline.load_master_snapshot_from_url(uuid, text, text, integer) to surge_worker_prod';
  execute 'alter function pipeline.load_master_snapshot_from_url(uuid, text, text, integer) set search_path = ''''';
end
$migration$;
