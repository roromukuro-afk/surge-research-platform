-- Phase 1.1: harden the object storage loader.
--
-- The previous loader accepted any URL together with an API key and sent that key
-- as an Authorization header, which is an SSRF and credential exfiltration path.
-- The loader now takes a signed URL only: no credential ever reaches the database,
-- the scheme must be https, and the host must be on an explicit allowlist.

create table if not exists pipeline.load_host_allowlist (
  host        text primary key,
  note        text,
  created_at  timestamptz not null default now()
);

comment on table pipeline.load_host_allowlist is
  'Hosts the database may fetch bulk snapshots from. Empty by default: nothing is fetchable until a host is registered.';

grant select on pipeline.load_host_allowlist to surge_worker_prod, surge_worker_research, surge_readonly;

alter table pipeline.load_host_allowlist enable row level security;

do $$
begin
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'load_host_allowlist' and policyname = 'surge_worker_prod_all') then
    create policy surge_worker_prod_all on pipeline.load_host_allowlist as permissive for all to surge_worker_prod using (true) with check (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'load_host_allowlist' and policyname = 'surge_readonly_read') then
    create policy surge_readonly_read on pipeline.load_host_allowlist as permissive for select to surge_readonly using (true);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'pipeline' and tablename = 'load_host_allowlist' and policyname = 'surge_worker_research_read') then
    create policy surge_worker_research_read on pipeline.load_host_allowlist as permissive for select to surge_worker_research using (true);
  end if;
end
$$;

-- Validation is a plain SQL function so it can be tested without the http extension.
create or replace function pipeline.assert_load_url_allowed(p_url text)
returns text
language plpgsql
stable
set search_path = ''
as $$
declare
  v_host text;
begin
  if p_url is null or p_url !~ '^https://' then
    raise exception 'snapshot url must use https: %', coalesce(p_url, '<null>');
  end if;

  v_host := lower(split_part(split_part(substring(p_url from 9), '/', 1), ':', 1));

  if not exists (select 1 from pipeline.load_host_allowlist h where h.host = v_host) then
    raise exception 'host not allowed for snapshot loading: %', v_host;
  end if;

  -- A signed URL carries its own short lived credential in the query string, so
  -- the database never needs to be handed an API key.
  if p_url !~* '[?&](token|x-amz-signature|signature)=' then
    raise exception 'snapshot url must be a signed url';
  end if;

  return v_host;
end;
$$;

comment on function pipeline.assert_load_url_allowed(text) is
  'Rejects non-https URLs, hosts that are not registered, and URLs that are not signed.';

grant execute on function pipeline.assert_load_url_allowed(text)
  to surge_worker_prod, surge_worker_research, surge_readonly;

drop function if exists pipeline.load_master_snapshot_from_url(uuid, text, text, integer);

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
        observed_at, available_at, source_data_version
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
             f[31]::timestamptz, f[32]::timestamptz, f[33]
      from (
        select string_to_array(btrim(line, chr(13)), chr(31)) as f
        from regexp_split_to_table(v_response.content, chr(10)) as line
        where length(btrim(line, chr(13))) > 0
      ) parsed
      where array_length(f, 1) = 33
      on conflict (run_id, source_id, source_record_id) do nothing;

      get diagnostics v_rows = row_count;
      return v_rows;
    end;
    $fn$;
  $sql$;

  execute $sql$
    comment on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) is
      'Loads a unit separator delimited snapshot from a signed object storage URL. No credential is passed to or stored in the database.'
  $sql$;

  execute 'grant execute on function pipeline.load_master_snapshot_from_signed_url(uuid, text, integer) to surge_worker_prod';
end
$migration$;
