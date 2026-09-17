-- Phase 4 follow-up: news gets its own raw-object manifest.
--
-- The first cut had news.purge_documents() read market.purge_targets, which
-- carries a foreign key into market.raw_objects. That table is correctly tied to
-- market.provider_license_policies and market.provider_plans by three foreign
-- keys, so putting a Bank of Japan press release in it would mean inventing an
-- entitlement plan and a market-data licence row for a central bank. CI found
-- this the moment the purge tests ran, which is the right place to find it.
--
-- So news keeps its own manifest, with its own licence link, and its own target
-- table. What stays shared is market.purge_requests: a purge is one obligation
-- ("this licence says delete what we hold from source X"), and splitting the
-- request itself would make it possible to satisfy half of one.

create table if not exists news.raw_documents (
  object_key text primary key,
  store_id text not null,
  source_key text not null references news.sources (source_key),
  policy_version text not null,
  sha256 text not null,
  bytes bigint,
  content_type text,
  request_url text,
  request_params jsonb,
  http_status integer,
  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',
  source_published_at timestamptz,
  ingested_at timestamptz not null default clock_timestamp(),
  run_id uuid references pipeline.runs (run_id),
  fetch_id uuid references pipeline.source_fetches (fetch_id),
  created_at timestamptz not null default clock_timestamp(),
  purged_at timestamptz,
  purge_request_id bigint references market.purge_requests (purge_request_id),

  constraint raw_documents_sha256_ck check (sha256 ~ '^[0-9a-f]{64}$'),
  constraint raw_documents_bytes_ck check (bytes is null or bytes >= 0),
  constraint raw_documents_licence_fk
    foreign key (source_key, policy_version)
    references news.source_policies (source_key, policy_version)
);

comment on table news.raw_documents is
  'What we actually fetched and stored, keyed by content address. The licence version is a foreign key rather than a note, so an object cannot exist without a recorded permission to hold it.';
comment on column news.raw_documents.object_key is
  'The content-addressed key in object storage: raw/<source>/<dataset>/<sha256>.<ext>. Write-once, so the same bytes fetched twice occupy one key.';
comment on column news.raw_documents.policy_version is
  'Which version of the source licence was in force when we stored this. A later policy change does not rewrite history; it decides what happens to the object next.';

create index if not exists raw_documents_source_idx on news.raw_documents (source_key, observed_at);
create index if not exists raw_documents_unpurged_idx on news.raw_documents (source_key) where purged_at is null;

-- Immutable apart from the purge columns, as market.raw_objects is.
create or replace function news.guard_raw_document_mutation() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'news.raw_documents is the deletion record; it must outlive the object (%). Set purged_at instead',
      old.object_key;
  end if;
  if (to_jsonb(new) - 'purged_at' - 'purge_request_id') <> (to_jsonb(old) - 'purged_at' - 'purge_request_id') then
    raise exception 'news.raw_documents is immutable apart from the purge columns (%)', old.object_key;
  end if;
  return new;
end;
$$;

drop trigger if exists news_raw_documents_immutable on news.raw_documents;
create trigger news_raw_documents_immutable
  before update or delete on news.raw_documents
  for each row execute function news.guard_raw_document_mutation();

-- A document may only point at an object we have a manifest row for.
alter table news.documents
  drop constraint if exists documents_raw_object_fk;
alter table news.documents
  add constraint documents_raw_object_fk
  foreign key (raw_object_key) references news.raw_documents (object_key);

comment on column news.documents.raw_object_key is
  'The stored object this document was parsed from, or null when only metadata was kept. Foreign-keyed, so a document cannot cite an object with no manifest entry.';

-- ---------------------------------------------------------------------------
-- Purge targets for news, sharing the market purge request
-- ---------------------------------------------------------------------------

create table if not exists news.purge_targets (
  purge_request_id bigint not null references market.purge_requests (purge_request_id),
  object_key text not null references news.raw_documents (object_key),
  store_id text not null,
  status market.purge_status not null default 'PENDING',
  detail text,
  resolved_at timestamptz,
  primary key (purge_request_id, object_key)
);

comment on table news.purge_targets is
  'Which stored news objects a purge request covers. Separate from market.purge_targets because the two manifests are separate; the request they answer to is the same one.';

create or replace function news.purge_documents(p_purge_request_id bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dry_run boolean;
  v_documents integer;
  v_objects integer;
begin
  select dry_run into v_dry_run
  from market.purge_requests
  where purge_request_id = p_purge_request_id;

  if not found then
    raise exception 'no such purge request %', p_purge_request_id;
  end if;

  if v_dry_run then
    select count(*) into v_documents
    from news.documents d
    join news.purge_targets t on t.object_key = d.raw_object_key
    where t.purge_request_id = p_purge_request_id;
    select count(*) into v_objects
    from news.purge_targets
    where purge_request_id = p_purge_request_id;
    return jsonb_build_object('dry_run', true, 'news_documents', v_documents, 'news_objects', v_objects);
  end if;

  perform set_config('news.purging', 'on', true);

  delete from news.documents d
  using news.purge_targets t
  where t.object_key = d.raw_object_key
    and t.purge_request_id = p_purge_request_id;
  get diagnostics v_documents = row_count;

  perform set_config('news.purging', 'off', true);

  -- The manifest row survives the object it describes. Losing it would lose the
  -- record that we ever held the thing, which is the opposite of what a purge
  -- obligation is trying to produce.
  update news.raw_documents r
     set purged_at = clock_timestamp(), purge_request_id = p_purge_request_id
    from news.purge_targets t
   where t.object_key = r.object_key
     and t.purge_request_id = p_purge_request_id
     and r.purged_at is null;
  get diagnostics v_objects = row_count;

  update news.purge_targets
     set status = 'DELETED', resolved_at = clock_timestamp()
   where purge_request_id = p_purge_request_id and status = 'PENDING';

  return jsonb_build_object('dry_run', false, 'news_documents', v_documents, 'news_objects', v_objects);
end;
$$;

comment on function news.purge_documents(bigint) is
  'Deletes the news rows a purge request covers and marks the manifest. Refuses to delete on a dry run, and is the only route past the append-only trigger on news.documents.';

revoke all on function news.purge_documents(bigint) from public;
grant execute on function news.purge_documents(bigint) to surge_purge;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table news.raw_documents, news.purge_targets from public;

grant select, insert on news.raw_documents to surge_worker_prod, surge_worker_research;
grant select on news.raw_documents to surge_readonly, surge_purge;

grant select on news.purge_targets to surge_readonly, surge_worker_prod, surge_worker_research;
grant select, insert, update on news.purge_targets to surge_purge;
