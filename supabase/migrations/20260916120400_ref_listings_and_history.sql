-- Phase 1: listings plus symbol / name / identifier / status history.
-- History rows are append-only: past tickers and delistings are never deleted.

create table ref.listings (
  listing_id           uuid primary key default gen_random_uuid(),
  security_id          uuid not null references ref.securities (security_id),
  exchange_id          text not null references ref.exchanges (exchange_id),
  local_code           text not null,
  market_segment_code  text,
  market_segment_name  text,
  is_primary           boolean not null default true,
  listing_status       ref.listing_status not null default 'UNKNOWN',
  listing_date         date,
  delisting_date       date,
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text,
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now(),
  constraint listings_effective_range_ck check (effective_to is null or effective_to > effective_from)
);

comment on table ref.listings is 'A security listed on an exchange. Delisted listings are kept so historical universes stay reproducible.';
create unique index listings_exchange_code_active_uq
  on ref.listings (exchange_id, local_code)
  where effective_to is null;
create index listings_security_idx on ref.listings (security_id);
create index listings_status_idx on ref.listings (listing_status);

create table ref.listing_symbols (
  listing_symbol_id    uuid primary key default gen_random_uuid(),
  listing_id           uuid not null references ref.listings (listing_id),
  symbol               text not null,
  symbol_type          text not null default 'TICKER',
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text,
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now(),
  unique (listing_id, symbol, symbol_type, effective_from)
);

comment on table ref.listing_symbols is 'Ticker history. A ticker change closes the old row (effective_to) and inserts a new one; rows are never deleted.';
create index listing_symbols_symbol_idx on ref.listing_symbols (symbol, symbol_type);

create table ref.security_names (
  security_name_id     uuid primary key default gen_random_uuid(),
  security_id          uuid not null references ref.securities (security_id),
  name                 text not null,
  normalized_name      text not null,
  name_type            text not null,
  language             text,
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text,
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now(),
  unique (security_id, name_type, name, effective_from)
);

comment on table ref.security_names is 'Legal / local / english / former names and aliases. Feeds future news entity linking.';
create index security_names_normalized_idx on ref.security_names (normalized_name);

create table ref.security_identifiers (
  identifier_id        uuid primary key default gen_random_uuid(),
  security_id          uuid not null references ref.securities (security_id),
  id_type              text not null,
  id_value             text not null,
  id_namespace         text,
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_id            text not null references pipeline.sources (source_id),
  source_record_id     text,
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now(),
  constraint security_identifiers_type_ck check (
    id_type in ('TICKER', 'LOCAL_CODE', 'CIK', 'ISIN', 'EDINET', 'LEI', 'CUSIP', 'FIGI', 'PROVIDER_ID')
  ),
  unique (security_id, id_type, id_namespace, id_value, effective_from)
);

comment on table ref.security_identifiers is 'Provider and registry identifiers (CIK, ISIN, EDINET code, provider ids). Extensible by id_type.';
create index security_identifiers_lookup_idx on ref.security_identifiers (id_type, id_value);

create table ref.listing_status_history (
  status_history_id    uuid primary key default gen_random_uuid(),
  listing_id           uuid not null references ref.listings (listing_id),
  listing_status       ref.listing_status not null,
  reason               text,
  effective_from       timestamptz not null,
  effective_to         timestamptz,
  observed_at          timestamptz not null,
  available_at         timestamptz not null,
  source_id            text not null references pipeline.sources (source_id),
  ingestion_run_id     uuid not null references pipeline.runs (run_id),
  created_at           timestamptz not null default now(),
  unique (listing_id, listing_status, effective_from)
);

comment on table ref.listing_status_history is 'Listing status over time, including delisting. Never deleted.';

-- As-of reconstruction: what the master looked like at a given knowledge time.
create or replace function ref.listings_as_of(p_available_at timestamptz)
returns table (
  listing_id uuid,
  security_id uuid,
  exchange_id text,
  local_code text,
  market_segment_code text,
  listing_status ref.listing_status,
  effective_from timestamptz,
  effective_to timestamptz
)
language sql
stable
as $$
  select l.listing_id,
         l.security_id,
         l.exchange_id,
         l.local_code,
         l.market_segment_code,
         l.listing_status,
         l.effective_from,
         l.effective_to
  from ref.listings l
  where l.available_at <= p_available_at
    and l.effective_from <= p_available_at
    and (l.effective_to is null or l.effective_to > p_available_at);
$$;

comment on function ref.listings_as_of(timestamptz) is
  'Reconstructs the listing master as it was known at p_available_at. Later corrections never leak into earlier points in time.';
