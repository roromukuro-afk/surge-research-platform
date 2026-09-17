-- Yanoshin TDnet WebAPI as a zero-cost JP timely-disclosure DISCOVERY source.
--
-- This reopens what D-122 closed, and the distinction it rests on is narrow
-- enough to be worth stating precisely.
--
-- **Yanoshin provides an index; TDnet provides the documents.** The operator
-- says so themselves: 「当サービスはインデックス情報を提供するものであり、提供元
-- サーバへの書類URLリンクを提供しております。書類データは提供元のサイトを必ず
-- ご確認ください。」 So metadata - who disclosed what, when, under which code,
-- and where the document lives - comes from Yanoshin and may be kept. The PDF
-- and XBRL themselves stay at TDnet, whose terms prohibit 複製, and being able
-- to reach an API that links to them licenses nothing about storing them.
--
-- Encoded rather than remembered: full_text_storage_allowed is PROHIBITED on
-- this source, so news.assert_storage_allows refuses a full-text write and the
-- collector never asks for a body it may not keep. The document URL is stored
-- because it is a link Yanoshin exists to provide; following it to archive the
-- file is a separate act this policy does not permit.
--
-- Verification of a disclosure's contents comes from somewhere that does permit
-- storage - the issuer's own IR feed, or EDINET - and links back to the same
-- material event as a VERIFICATION source.

-- ---------------------------------------------------------------------------
-- The source and its policy
-- ---------------------------------------------------------------------------

insert into news.sources
  (source_key, name, scope, source_kind, access_mechanism, official_url, feed_url, docs_url,
   auth_requirement, required_env, documented_rate_limit, min_request_interval_seconds,
   discovery_role, verification_role, fetch_priority, enabled, notes)
values
  ('yanoshin_tdnet', 'Yanoshin TDnet WebAPI（非公式・Discovery）', 'JP', 'OFFICIAL_DISCLOSURE', 'REST_API',
   'https://webapi.yanoshin.jp/',
   'https://webapi.yanoshin.jp/webapi/tdnet/list/recent.json',
   'https://webapi.yanoshin.jp/llms.txt',
   'NONE', '{}',
   'none published. llms.txt says only "No rate limit is explicitly enforced, but please be reasonable with request frequency", so the interval below is politeness rather than compliance.',
   0.3,
   -- Discovery only. It may not verify anything: it is an unofficial index of a
   -- source we cannot read directly, so a second, storable source has to confirm
   -- what a disclosure actually said.
   true, false, 15, true,
   'Reopens D-122 for DISCOVERY and METADATA only. The TDnet document body is NOT covered and is never archived on the strength of this API. robots.txt disallows *.json, *.xml, *.rss and *.atom to Googlebot specifically and allows everything to every other agent.')
on conflict (source_key) do nothing;

insert into news.source_policies
  (source_key, policy_version, license_mode,
   full_text_storage_allowed, metadata_storage_allowed, derived_output_sharing_allowed,
   raw_redistribution_allowed, commercial_use_allowed, attribution_required, delete_on_cancel,
   robots_allows_path, robots_checked_at, crawl_delay_seconds, deciding_clause, terms_url, terms_checked_at, notes)
values
  ('yanoshin_tdnet', 'yanoshin-2026-09-17', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   -- PROHIBITED refers to the TDnet document body, not to the index. It is set
   -- here so the storage assertion refuses before anything is written.
   'PROHIBITED', 'ALLOWED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED', 'REQUIRED', 'NOT_SPECIFIED',
   'ALLOWED', now(), 0.3,
   '当サービスはインターネットを利用した情報収集、分析を目的としたものです。当サービスはインデックス情報を提供するものであり、提供元サーバへの書類URLリンクを提供しております。書類データは提供元のサイトを必ずご確認ください。 — the operator frames the service as index information plus links, and directs the reader to the originating site for the documents themselves.',
   'https://webapi.yanoshin.jp/', now(),
   'The index may be kept; the TDnet body may not, and no amount of access to this API changes that. Attribution required as a courtesy to an unofficial free service. robots.txt: "User-agent: Googlebot / Disallow: /*.json ..." then "User-Agent: * / Allow:/" - the machine-readable endpoints are closed to Googlebot and open to everyone else, so our collector is permitted.')
on conflict (source_key, policy_version) do nothing;

-- ---------------------------------------------------------------------------
-- The index rows
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'code_normalisation' and n.nspname = 'news') then
    create type news.code_normalisation as enum (
      'FIVE_CHAR_TRAILING_ZERO_STRIPPED',  -- 72030 -> 7203, 130A0 -> 130A
      'FIVE_CHAR_KEPT',                    -- 587A4, 13264: the 5th character is not 0
      'ALREADY_FOUR_CHAR',
      'UNEXPECTED_SHAPE'
    );
  end if;
end;
$$;

create table if not exists news.tdnet_items (
  document_id uuid primary key references news.documents (document_id) on delete cascade,
  yanoshin_id bigint not null,
  pubdate timestamptz not null,

  -- Both forms, always. The raw code is what the service said; the normalised
  -- one is what we matched against. Keeping only the second would make a
  -- mis-normalisation undetectable after the fact.
  raw_company_code text not null,
  normalised_company_code text not null,
  code_normalisation news.code_normalisation not null,

  company_name text,
  title text not null,
  document_url text,
  url_xbrl text,
  markets_string text,
  update_history text,

  -- Which security the code resolved to, if it resolved at all.
  security_id uuid,
  listing_market_code ref.market_code,
  mapping_confidence text,
  unmapped_reason text,

  -- Provenance of the fetch itself.
  source_endpoint text not null,
  raw_response_sha256 text not null,
  system_first_seen_at timestamptz not null,
  ingested_at timestamptz not null,
  available_to_model_at timestamptz not null,

  created_at timestamptz not null default clock_timestamp(),

  constraint tdnet_items_raw_code_present check (length(btrim(raw_company_code)) > 0),
  constraint tdnet_items_response_hash check (raw_response_sha256 ~ '^[0-9a-f]{64}$'),
  constraint tdnet_items_seen_before_ingested check (system_first_seen_at <= ingested_at),
  constraint tdnet_items_ingested_before_available check (ingested_at <= available_to_model_at),
  -- A five-character code whose fifth character is not "0" is very likely an
  -- ETF, ETN or other non-ordinary security. Stripping it would silently merge
  -- it with a different issuer, so the enum records which branch was taken and
  -- this check keeps the two in step.
  constraint tdnet_items_normalisation_agrees check (
    (code_normalisation = 'FIVE_CHAR_TRAILING_ZERO_STRIPPED'
       and length(raw_company_code) = 5 and right(raw_company_code, 1) = '0'
       and normalised_company_code = left(raw_company_code, 4))
    or (code_normalisation = 'FIVE_CHAR_KEPT'
       and length(raw_company_code) = 5 and right(raw_company_code, 1) <> '0'
       and normalised_company_code = raw_company_code)
    or (code_normalisation = 'ALREADY_FOUR_CHAR'
       and length(raw_company_code) = 4 and normalised_company_code = raw_company_code)
    or code_normalisation = 'UNEXPECTED_SHAPE'
  ),
  unique (yanoshin_id, raw_response_sha256)
);

comment on table news.tdnet_items is
  'The Yanoshin index rows, one per news.documents row. Metadata only: the TDnet PDF and XBRL behind document_url and url_xbrl are never archived here, because being able to reach an index that links to them licenses nothing about storing them.';
comment on column news.tdnet_items.raw_company_code is
  'Exactly what the service returned, always five characters in practice. Kept because a normalisation that turns out to be wrong is only findable if the input survives.';
comment on column news.tdnet_items.code_normalisation is
  'Which branch of the rule applied. Trailing zeros are NOT stripped unconditionally: 72030 becomes 7203 and 130A0 becomes 130A, but 587A4 and 13264 keep all five characters because a non-zero fifth character marks a non-ordinary security.';
comment on column news.tdnet_items.document_url is
  'The link to the disclosure at TDnet, stored because providing links is what this service is for. Following it to archive the file is a different act, and one this source''s policy does not permit.';
comment on column news.tdnet_items.yanoshin_id is
  'The service''s own item id. Assigned in insert order rather than publication order - an item can carry a higher id and an earlier pubdate - which is exactly why it works as a high-water mark for new items and does not work as a sort key for time.';
comment on column news.tdnet_items.raw_response_sha256 is
  'Hash of the whole response the row came from, so a re-fetch that returns changed content produces a new row rather than overwriting the old one.';

create index if not exists tdnet_items_yanoshin_id_idx on news.tdnet_items (yanoshin_id desc);
create index if not exists tdnet_items_code_idx on news.tdnet_items (normalised_company_code, pubdate desc);
create index if not exists tdnet_items_unmapped_idx on news.tdnet_items (raw_company_code)
  where security_id is null;

-- ---------------------------------------------------------------------------
-- Cursor and coverage
-- ---------------------------------------------------------------------------

create table if not exists news.tdnet_coverage (
  coverage_id bigint generated always as identity primary key,
  run_id uuid references pipeline.runs (run_id),
  as_of_date date not null,
  collected_at timestamptz not null default clock_timestamp(),

  items_retrieved integer not null default 0,
  unique_items integer not null default 0,
  new_items integer not null default 0,
  duplicate_items integer not null default 0,
  unmapped_company_codes integer not null default 0,
  document_url_present integer not null default 0,
  xbrl_url_present integer not null default 0,
  verification_source_found integer not null default 0,
  metadata_only_events integer not null default 0,
  fetch_errors integer not null default 0,
  last_seen_id bigint,
  collection_lag_seconds numeric(12, 3),

  endpoint text,
  notes text
);

comment on table news.tdnet_coverage is
  'Per collection pass. collection_lag_seconds is the gap between the newest pubdate we retrieved and the moment we retrieved it - the honest measure of how stale this system''s view of the disclosure feed is, and the number that decides whether a polling interval is adequate.';
comment on column news.tdnet_coverage.unmapped_company_codes is
  'Codes the security master could not resolve. Counted separately from errors because an unmapped code is usually a real security we do not carry - a newly listed issuer, an ETF - rather than a fault.';
comment on column news.tdnet_coverage.metadata_only_events is
  'Events for which no storable verification source was found, so all we hold is the index row. Tracked because a material event we can see but cannot read is a different thing from one we have.';

create index if not exists tdnet_coverage_date_idx on news.tdnet_coverage (as_of_date desc);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table news.tdnet_items, news.tdnet_coverage from public;

grant select, insert on news.tdnet_items to surge_worker_prod, surge_worker_research;
grant select on news.tdnet_items to surge_readonly, surge_purge;
-- The mapping is filled in after the row is written, once the security master
-- has been consulted; nothing else about the row may change.
grant update (security_id, listing_market_code, mapping_confidence, unmapped_reason)
  on news.tdnet_items to surge_worker_prod, surge_worker_research;

grant select, insert on news.tdnet_coverage to surge_worker_prod, surge_worker_research;
grant select on news.tdnet_coverage to surge_readonly;

grant usage on all sequences in schema news to surge_worker_prod, surge_worker_research;
