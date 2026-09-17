-- Phase 4: news and disclosure collection.
--
-- The hard problem in this schema is not storage, it is time. A news item has
-- four distinct timestamps and conflating any two of them silently teaches the
-- model to see the future:
--
--   source_published_at    when the source published it
--   system_first_seen_at   when this system first observed that it existed
--   ingested_at            when this system stored its content
--   available_to_model_at  when the model is permitted to use it
--
-- Only the last one gates a decision. source_published_at is a property of the
-- world, not of our knowledge, and using it as the availability time is the
-- single easiest way to backtest a strategy that could never have been run.

create schema if not exists news;
comment on schema news is
  'News, disclosure and official-source documents, with the knowledge times that decide when a model may use them.';

-- Register the schema with the privilege guards BEFORE creating any function in
-- it. The event trigger that strips PUBLIC EXECUTE reads this list at creation
-- time, so a schema added afterwards would leave its own functions open.
create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market', 'screening', 'news'];
$$;

alter default privileges in schema news
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema news
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;
alter default privileges in schema news
  grant execute on functions to current_user;
alter default privileges in schema news
  revoke execute on functions from public;

-- ---------------------------------------------------------------------------
-- Vocabulary
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'source_scope' and n.nspname = 'news') then
    create type news.source_scope as enum ('JP', 'US', 'GLOBAL');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'source_kind' and n.nspname = 'news') then
    create type news.source_kind as enum (
      'OFFICIAL_DISCLOSURE',   -- TDnet, EDINET: the issuer's own regulated filing
      'REGULATOR',             -- SEC, FSA
      'EXCHANGE',              -- JPX, Nasdaq
      'CENTRAL_BANK',          -- BOJ, Federal Reserve
      'GOVERNMENT',            -- ministries, agencies, White House
      'COMPANY_IR',            -- the company's own investor-relations feed
      'VENDOR_WIRE',           -- Business Wire, PR Newswire and similar
      'NEWS_MEDIA'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'access_mechanism' and n.nspname = 'news') then
    create type news.access_mechanism as enum (
      'REST_API', 'RSS', 'ATOM', 'BULK_FILE', 'HTML_PAGE', 'EMAIL'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'auth_requirement' and n.nspname = 'news') then
    create type news.auth_requirement as enum (
      'NONE', 'FREE_API_KEY', 'PAID_API_KEY', 'ACCOUNT_REQUIRED', 'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'document_type' and n.nspname = 'news') then
    create type news.document_type as enum (
      'TIMELY_DISCLOSURE',     -- 適時開示
      'STATUTORY_FILING',      -- EDINET / EDGAR filings
      'PRESS_RELEASE',
      'POLICY_STATEMENT',      -- central bank / government policy
      'STATISTIC_RELEASE',
      'IR_RELEASE',
      'NEWS_ARTICLE',
      'OTHER'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'time_precision' and n.nspname = 'news') then
    create type news.time_precision as enum (
      'EXACT',        -- the source gave a timestamp we can trust to the minute
      'DATE_ONLY',    -- the source gave a date; the time of day is not known
      'INFERRED',     -- we derived it (e.g. from an HTTP Last-Modified header)
      'UNKNOWN'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'body_storage' and n.nspname = 'news') then
    create type news.body_storage as enum (
      'FULL_TEXT',        -- the licence permits keeping the whole document
      'METADATA_ONLY',    -- only title, URL and identifiers are kept
      'EXCERPT',          -- a bounded excerpt is kept
      'NOT_STORED'
    );
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Source registry (configuration; the runtime worker may only read it)
-- ---------------------------------------------------------------------------

create table if not exists news.sources (
  source_key text primary key,
  name text not null,
  scope news.source_scope not null,
  source_kind news.source_kind not null,
  access_mechanism news.access_mechanism not null,
  official_url text not null,
  feed_url text,
  docs_url text,
  auth_requirement news.auth_requirement not null default 'NONE',
  required_env text[] not null default '{}',
  documented_rate_limit text,
  min_request_interval_seconds numeric(10, 3) not null default 1.0,
  discovery_role boolean not null default true,
  verification_role boolean not null default false,
  fetch_priority integer not null default 100,
  enabled boolean not null default false,
  live_verified_at timestamptz,
  notes text,
  created_at timestamptz not null default clock_timestamp()
);

comment on table news.sources is
  'Every source the collector may talk to. Configuration, not runtime state: the worker reads it and cannot change it.';
comment on column news.sources.discovery_role is
  'May this source be used to DISCOVER that an event happened? Separate from verification on purpose - a source can be fast and unreliable, or slow and authoritative, and those are different jobs.';
comment on column news.sources.verification_role is
  'May this source be used to CONFIRM an event another source discovered? A source can hold both roles.';
comment on column news.sources.fetch_priority is
  'Ordering for the FETCH SCHEDULE only - which source the collector polls first when it is behind. It is deliberately NOT a measure of how strong a material is. Ranking IR above news by construction is forbidden (CLAUDE.md 1-12); material strength is evaluated per event in Phase 5 from novelty, surprise, directness, magnitude, persistence, market reaction and priced-in.';
comment on column news.sources.required_env is
  'Environment variable names this source needs. A job whose required_env is unset is SKIPPED, not FAILED - a missing credential must not look like a broken pipeline.';
comment on column news.sources.live_verified_at is
  'When this source was last actually fetched successfully. Null means IMPLEMENTED_NOT_LIVE_VERIFIED: the adapter exists and is tested against fixtures, but has never spoken to the real service.';

-- ---------------------------------------------------------------------------
-- Licensing, with the same four-state vocabulary Phase 2 uses for market data
-- ---------------------------------------------------------------------------

create table if not exists news.source_policies (
  source_key text not null references news.sources (source_key),
  policy_version text not null,
  license_mode market.license_mode not null,
  -- The axis that decides everything for news: may we keep the words?
  full_text_storage_allowed market.license_permission not null default 'UNKNOWN',
  metadata_storage_allowed market.license_permission not null default 'UNKNOWN',
  derived_output_sharing_allowed market.license_permission not null default 'UNKNOWN',
  raw_redistribution_allowed market.license_permission not null default 'UNKNOWN',
  commercial_use_allowed market.license_permission not null default 'UNKNOWN',
  attribution_required market.license_obligation not null default 'UNKNOWN',
  delete_on_cancel market.license_obligation not null default 'UNKNOWN',
  robots_allows_path market.license_permission not null default 'UNKNOWN',
  robots_checked_at timestamptz,
  crawl_delay_seconds numeric(10, 3),
  deciding_clause text,
  terms_url text,
  terms_checked_at timestamptz,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  notes text,
  primary key (source_key, policy_version)
);

comment on table news.source_policies is
  'What each source permits, in the four-state vocabulary (ALLOWED / PROHIBITED / NOT_SPECIFIED / UNKNOWN). Silence in the terms is NOT_SPECIFIED, never ALLOWED - inventing a permission is how a research project becomes a licence breach.';
comment on column news.source_policies.full_text_storage_allowed is
  'Whether the document body may be kept. When this is anything other than ALLOWED the collector stores METADATA_ONLY and says so on the row, rather than quietly keeping the text anyway.';
comment on column news.source_policies.deciding_clause is
  'The sentence the verdict turns on, quoted. A policy row without one is an opinion.';

-- Append-only: a policy is superseded by a new version, never edited.
create or replace function news.forbid_policy_mutation() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'news.source_policies is append-only; supersede with a new policy_version instead of deleting %/%',
      old.source_key, old.policy_version;
  end if;
  -- Closing a policy is the one legitimate update.
  if old.effective_to is not null or new.effective_to is null then
    raise exception 'news.source_policies is append-only; the only permitted update is setting effective_to once (%/%)',
      old.source_key, old.policy_version;
  end if;
  if (to_jsonb(new) - 'effective_to') <> (to_jsonb(old) - 'effective_to') then
    raise exception 'news.source_policies: closing a policy may not change any other column (%/%)',
      old.source_key, old.policy_version;
  end if;
  return new;
end;
$$;

drop trigger if exists news_source_policies_append_only on news.source_policies;
create trigger news_source_policies_append_only
  before update or delete on news.source_policies
  for each row execute function news.forbid_policy_mutation();

create or replace function news.assert_storage_allows(
  p_source_key text,
  p_body_storage news.body_storage,
  p_at timestamptz default clock_timestamp()
) returns void
language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare
  v_policy news.source_policies%rowtype;
begin
  select * into v_policy
  from news.source_policies
  where source_key = p_source_key
    and effective_from <= p_at
    and (effective_to is null or effective_to > p_at)
  order by effective_from desc
  limit 1;

  if not found then
    raise exception 'no licence policy for news source % at %; refusing to store anything', p_source_key, p_at;
  end if;

  if p_body_storage = 'NOT_STORED' then
    return;
  end if;

  if p_body_storage = 'FULL_TEXT' and v_policy.full_text_storage_allowed <> 'ALLOWED' then
    raise exception 'source % does not permit full-text storage (full_text_storage_allowed = %)',
      p_source_key, v_policy.full_text_storage_allowed;
  end if;

  if v_policy.metadata_storage_allowed <> 'ALLOWED' then
    raise exception 'source % does not permit even metadata storage (metadata_storage_allowed = %)',
      p_source_key, v_policy.metadata_storage_allowed;
  end if;
end;
$$;

comment on function news.assert_storage_allows(text, news.body_storage, timestamptz) is
  'Raises unless the source explicitly permits the requested storage depth. Anything but ALLOWED raises, including NOT_SPECIFIED and UNKNOWN.';

-- ---------------------------------------------------------------------------
-- Documents: the four times, and the rule that backfill is not hindsight
-- ---------------------------------------------------------------------------

create table if not exists news.documents (
  document_id uuid primary key default gen_random_uuid(),
  source_key text not null references news.sources (source_key),
  source_document_id text not null,
  document_url text,
  document_type news.document_type not null,
  title text,
  language text,

  -- 1. When the source says it published this.
  source_published_at timestamptz,
  source_published_precision news.time_precision not null default 'UNKNOWN',
  -- 2. When we first saw that it existed (e.g. it appeared in a feed listing).
  system_first_seen_at timestamptz not null,
  -- 3. When we stored its content.
  ingested_at timestamptz not null,
  -- 4. When a model may use it. This is the only one that gates a decision.
  available_to_model_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',

  -- Research-only. Never read by production; see news.documents_as_of().
  replay_assumed_available_at timestamptz,
  replay_assumption_note text,

  content_sha256 text not null,
  raw_object_key text,
  body_storage news.body_storage not null,
  body_storage_reason text,
  body_text text,
  byte_size bigint,

  run_id uuid references pipeline.runs (run_id),
  fetch_id uuid references pipeline.source_fetches (fetch_id),

  revision_seq integer not null default 1,
  supersedes_document_id uuid references news.documents (document_id),

  created_at timestamptz not null default clock_timestamp(),

  constraint documents_seen_before_ingested check (system_first_seen_at <= ingested_at),
  constraint documents_ingested_before_available check (ingested_at <= available_to_model_at),
  constraint documents_revision_positive check (revision_seq >= 1),
  -- A body may only be present when the row says a body is stored.
  constraint documents_body_matches_storage check (
    (body_storage in ('FULL_TEXT', 'EXCERPT') and body_text is not null)
    or (body_storage in ('METADATA_ONLY', 'NOT_STORED') and body_text is null)
  ),
  -- The replay assumption is research scaffolding and must be labelled as such.
  constraint documents_replay_needs_note check (
    replay_assumed_available_at is null or replay_assumption_note is not null
  ),
  unique (source_key, source_document_id, content_sha256)
);

comment on table news.documents is
  'One row per (source, document, content hash). An amended document does not overwrite: it arrives as a new row with revision_seq + 1 and supersedes_document_id pointing at the previous one.';
comment on column news.documents.available_to_model_at is
  'The honest knowledge time. For backfill this is the backfill time, NOT the publication time - a document fetched today was not knowable last year, and pretending otherwise is the leak this column exists to prevent.';
comment on column news.documents.replay_assumed_available_at is
  'Research-only counterfactual: when we ASSUME we would have seen it, had the collector been running. Production never reads this. It exists so replay experiments can state their assumption explicitly instead of smuggling it into available_to_model_at.';
comment on column news.documents.source_published_precision is
  'Many official sources publish a date with no time. Recording DATE_ONLY stops a later reader from treating midnight as a real publication minute.';
comment on column news.documents.body_storage is
  'How much of the document we kept, decided by the source licence rather than by convenience.';

create index if not exists documents_available_idx
  on news.documents (available_to_model_at, source_key);
create index if not exists documents_source_doc_idx
  on news.documents (source_key, source_document_id, revision_seq);
create index if not exists documents_published_idx
  on news.documents (source_published_at) where source_published_at is not null;

-- Documents are append-only. Corrections arrive as new revisions.
--
-- Deletion has exactly one legitimate cause - a licence obliging us to remove
-- the data - and exactly one legitimate route: news.purge_documents(), which
-- runs as the purge principal and sets the session flag below. The flag is
-- transaction-local (set local), so it cannot leak into another statement, and
-- the ingestion worker has no way to set it because it cannot execute the
-- SECURITY DEFINER function's body as itself.
create or replace function news.forbid_document_mutation() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
begin
  if tg_op = 'DELETE' then
    if coalesce(current_setting('news.purging', true), 'off') = 'on' then
      return old;
    end if;
    raise exception
      'news.documents is append-only; licence-driven deletion must go through news.purge_documents() (document %)',
      old.document_id;
  end if;
  raise exception 'news.documents is append-only; store a new revision instead of updating document %', old.document_id;
end;
$$;

drop trigger if exists news_documents_append_only on news.documents;
create trigger news_documents_append_only
  before update or delete on news.documents
  for each row execute function news.forbid_document_mutation();

-- The production read path. It cannot see the replay columns, by construction.
create or replace function news.documents_as_of(p_knowledge_cutoff timestamptz)
returns table (
  document_id uuid,
  source_key text,
  source_document_id text,
  document_url text,
  document_type news.document_type,
  title text,
  language text,
  source_published_at timestamptz,
  source_published_precision news.time_precision,
  available_to_model_at timestamptz,
  availability_basis market.availability_basis,
  body_storage news.body_storage,
  body_text text,
  content_sha256 text,
  revision_seq integer
)
language sql stable security invoker set search_path = pg_catalog, public as $$
  select d.document_id, d.source_key, d.source_document_id, d.document_url, d.document_type,
         d.title, d.language, d.source_published_at, d.source_published_precision,
         d.available_to_model_at, d.availability_basis, d.body_storage, d.body_text,
         d.content_sha256, d.revision_seq
  from news.documents d
  where d.available_to_model_at <= p_knowledge_cutoff;
$$;

comment on function news.documents_as_of(timestamptz) is
  'The only read path production may use. Filters on available_to_model_at and returns no replay column, so a caller cannot accidentally read a counterfactual assumption as fact. Note that clock_timestamp() - not now() - is the right argument inside a transaction, since now() is transaction start.';

-- The purge path. Mirrors market.purge_market_rows(): it refuses to delete on a
-- dry run, and it reports what it removed rather than returning quietly.
create or replace function news.purge_documents(p_purge_request_id bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dry_run boolean;
  v_count integer;
begin
  select dry_run into v_dry_run
  from market.purge_requests
  where purge_request_id = p_purge_request_id;

  if not found then
    raise exception 'no such purge request %', p_purge_request_id;
  end if;

  if v_dry_run then
    select count(*) into v_count
    from news.documents d
    join market.purge_targets t on t.object_key = d.raw_object_key
    where t.purge_request_id = p_purge_request_id;
    return jsonb_build_object('dry_run', true, 'news_documents', v_count);
  end if;

  perform set_config('news.purging', 'on', true);

  delete from news.documents d
  using market.purge_targets t
  where t.object_key = d.raw_object_key
    and t.purge_request_id = p_purge_request_id;
  get diagnostics v_count = row_count;

  perform set_config('news.purging', 'off', true);

  return jsonb_build_object('dry_run', false, 'news_documents', v_count);
end;
$$;

comment on function news.purge_documents(bigint) is
  'Deletes the news rows a purge request covers. Runs as the purge principal, refuses to delete on a dry run, and is the only route past the append-only trigger.';

revoke all on function news.purge_documents(bigint) from public;
grant execute on function news.purge_documents(bigint) to surge_purge;

-- ---------------------------------------------------------------------------
-- Incremental collection state
-- ---------------------------------------------------------------------------

create table if not exists news.fetch_cursors (
  source_key text not null references news.sources (source_key),
  cursor_name text not null,
  cursor_value text,
  last_success_at timestamptz,
  last_attempt_at timestamptz,
  consecutive_failures integer not null default 0,
  etag text,
  last_modified text,
  updated_at timestamptz not null default clock_timestamp(),
  primary key (source_key, cursor_name)
);

comment on table news.fetch_cursors is
  'High-water marks so incremental collection resumes without refetching everything, plus the conditional-request headers that let a polite collector get a 304 instead of a body.';

create table if not exists news.source_coverage (
  coverage_id bigint generated always as identity primary key,
  run_id uuid not null references pipeline.runs (run_id),
  source_key text not null references news.sources (source_key),
  as_of_date date not null,
  items_listed integer not null default 0,
  items_new integer not null default 0,
  items_duplicate integer not null default 0,
  items_revised integer not null default 0,
  items_skipped_licence integer not null default 0,
  fetch_errors integer not null default 0,
  parse_errors integer not null default 0,
  quality_warnings integer not null default 0,
  coverage_quality market.coverage_quality not null default 'UNKNOWN',
  notes text,
  created_at timestamptz not null default clock_timestamp(),
  unique (run_id, source_key, as_of_date)
);

comment on table news.source_coverage is
  'Per source per day. Fetch errors, parse errors and licence skips are counted separately because they call for different fixes: a fetch error is an outage, a parse error is our bug, and a licence skip is working as intended.';

-- ---------------------------------------------------------------------------
-- Grants. Default privileges in this project are read-only, so anything the
-- runtime writes has to be granted explicitly - and never DELETE.
-- ---------------------------------------------------------------------------

revoke all on all tables in schema news from public;
grant usage on schema news to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

grant select on news.sources, news.source_policies to
  surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

grant select, insert on news.documents to surge_worker_prod, surge_worker_research;
grant select on news.documents to surge_readonly, surge_purge;

grant select, insert, update on news.fetch_cursors to surge_worker_prod, surge_worker_research;
grant select on news.fetch_cursors to surge_readonly;

grant select, insert on news.source_coverage to surge_worker_prod, surge_worker_research;
grant select on news.source_coverage to surge_readonly;

grant usage on all sequences in schema news to surge_worker_prod, surge_worker_research;

grant execute on function news.documents_as_of(timestamptz) to
  surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function news.assert_storage_allows(text, news.body_storage, timestamptz) to
  surge_worker_prod, surge_worker_research;

-- No role holds DELETE on news.documents, not even surge_purge. Deletion happens
-- inside news.purge_documents(), which runs as its owner. Granting surge_purge a
-- direct DELETE would be a false affordance: the append-only trigger would
-- reject it anyway, and the grant would suggest a route that does not exist.
