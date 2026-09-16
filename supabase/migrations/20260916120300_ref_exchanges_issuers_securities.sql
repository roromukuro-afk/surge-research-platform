-- Phase 1: exchange master, issuer identity and security identity.
-- Tickers are never used as the permanent identifier; security_id is.

create table ref.exchanges (
  exchange_id          text primary key,
  market_code          ref.market_code not null,
  mic                  text,
  name                 text not null,
  country              text not null,
  timezone             text not null,
  is_target            boolean not null default false,
  notes                text,
  created_at           timestamptz not null default now()
);

comment on table ref.exchanges is 'Explicit exchange master. Universe membership is decided from this, never from ticker suffixes.';
comment on column ref.exchanges.is_target is 'True when the exchange is in scope for the current universe definition.';

create table ref.issuers (
  issuer_id            uuid primary key default gen_random_uuid(),
  country              text not null,
  legal_name           text not null,
  normalized_name      text not null,
  name_source          text,
  created_at           timestamptz not null default now()
);

comment on table ref.issuers is 'Issuing entity. One issuer can have several securities and listings.';
create index issuers_normalized_name_idx on ref.issuers (normalized_name);

create table ref.securities (
  security_id              uuid primary key default gen_random_uuid(),
  issuer_id                uuid not null references ref.issuers (issuer_id),
  market_code              ref.market_code not null,
  security_type            ref.security_type not null,
  security_type_source     text,
  security_type_evidence   jsonb not null default '{}'::jsonb,
  is_adr                   boolean not null default false,
  adr_underlying_country   text,
  adr_underlying_issuer_id uuid references ref.issuers (issuer_id),
  is_spac_pre_merger       boolean,
  currency                 text not null,
  first_seen_at            timestamptz not null,
  last_seen_at             timestamptz not null,
  created_at               timestamptz not null default now()
);

comment on table ref.securities is 'Internal security identity (internal_security_id). Delisted securities are retained forever to avoid survivorship bias.';
comment on column ref.securities.is_spac_pre_merger is 'NULL means unknown; unknown is never silently treated as false when it changes universe membership.';

create index securities_issuer_idx on ref.securities (issuer_id);
create index securities_market_type_idx on ref.securities (market_code, security_type);
