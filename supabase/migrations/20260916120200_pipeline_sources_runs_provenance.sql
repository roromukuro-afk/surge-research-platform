-- Phase 1: ingestion runs, data sources and provenance.

create table pipeline.sources (
  source_id            text primary key,
  name                 text not null,
  market_code          ref.market_code,
  source_kind          text not null,
  base_url             text,
  terms_url            text,
  terms_checked_at     date,
  full_text_allowed    boolean,
  notes                text,
  created_at           timestamptz not null default now()
);

comment on table pipeline.sources is 'Registry of external data sources. No fixed predictive ranking is attached to a source.';
comment on column pipeline.sources.terms_checked_at is 'Date the usage terms were last reviewed before writing collection code.';

create table pipeline.runs (
  run_id               uuid primary key default gen_random_uuid(),
  job_name             text not null,
  job_version          text not null,
  run_mode             pipeline.run_mode not null,
  market_code          ref.market_code,
  idempotency_key      text not null unique,
  status               pipeline.run_status not null default 'RUNNING',
  started_at           timestamptz not null default now(),
  finished_at          timestamptz,
  data_cutoff          timestamptz,
  as_of_date           date,
  git_sha              text,
  config_hash          text,
  versions             jsonb not null default '{}'::jsonb,
  provider_bindings    jsonb not null default '{}'::jsonb,
  params               jsonb not null default '{}'::jsonb,
  runner_id            text,
  created_at           timestamptz not null default now()
);

comment on table pipeline.runs is 'One row per job execution; every stored result references the run that produced it.';
comment on column pipeline.runs.idempotency_key is 'Prevents double execution regardless of which JobRunner implementation is used.';

create index runs_job_started_idx on pipeline.runs (job_name, started_at desc);

create table pipeline.source_fetches (
  fetch_id             uuid primary key default gen_random_uuid(),
  run_id               uuid not null references pipeline.runs (run_id),
  source_id            text not null references pipeline.sources (source_id),
  endpoint             text not null,
  request_params       jsonb not null default '{}'::jsonb,
  requested_at         timestamptz not null,
  received_at          timestamptz,
  http_status          integer,
  item_count           integer,
  bytes                bigint,
  content_sha256       text,
  raw_object_key       text,
  source_published_at  timestamptz,
  observed_at          timestamptz,
  available_at         timestamptz,
  error_message        text,
  created_at           timestamptz not null default now()
);

comment on table pipeline.source_fetches is 'Provenance for every provider call: what was requested, what came back, and when it became usable.';
comment on column pipeline.source_fetches.available_at is 'When the fetched data became usable by analysis. Backfills carry the backfill time, not the original publication time.';

create index source_fetches_run_idx on pipeline.source_fetches (run_id);
create index source_fetches_source_idx on pipeline.source_fetches (source_id, requested_at desc);

create table pipeline.run_errors (
  error_id             uuid primary key default gen_random_uuid(),
  run_id               uuid not null references pipeline.runs (run_id),
  stage                text not null,
  severity             text not null default 'ERROR',
  error_type           text,
  message              text not null,
  context              jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now()
);

create index run_errors_run_idx on pipeline.run_errors (run_id);
