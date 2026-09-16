-- Phase 1: versioned universe definitions, per-security decisions and coverage.

create table universe.definitions (
  universe_version     text primary key,
  spec_path            text not null,
  spec_sha256          text not null,
  description          text,
  effective_from       date not null,
  effective_to         date,
  created_at           timestamptz not null default now()
);

comment on table universe.definitions is 'Versioned universe definitions. Changing the rules means a new version, never an edit of an existing one.';

create table universe.decision_reasons (
  reason_code          text primary key,
  decision             universe.decision not null,
  description          text not null
);

comment on table universe.decision_reasons is 'Controlled vocabulary for why a security is included, excluded or left unresolved.';

insert into universe.decision_reasons (reason_code, decision, description) values
  ('TARGET_MARKET_COMMON_STOCK', 'INCLUDED',   'Common stock listed on a target exchange / segment.'),
  ('ELIGIBLE_ADR',               'INCLUDED',   'ADR judged eligible under the universe definition.'),
  ('NOT_TARGET_EXCHANGE',        'EXCLUDED',   'Listed outside the target exchanges of this universe version.'),
  ('NOT_TARGET_SEGMENT',         'EXCLUDED',   'Market segment is out of scope (e.g. TOKYO PRO Market).'),
  ('ETF',                        'EXCLUDED',   'Exchange traded fund.'),
  ('ETN',                        'EXCLUDED',   'Exchange traded note.'),
  ('REIT_OR_FUND',               'EXCLUDED',   'REIT, infrastructure fund or similar fund vehicle.'),
  ('PREFERRED',                  'EXCLUDED',   'Preferred share.'),
  ('WARRANT',                    'EXCLUDED',   'Warrant.'),
  ('RIGHT',                      'EXCLUDED',   'Right.'),
  ('UNIT',                       'EXCLUDED',   'Unit (typically SPAC unit).'),
  ('OTC',                        'EXCLUDED',   'Traded over the counter.'),
  ('SPAC_PRE_MERGER',            'EXCLUDED',   'Pre-merger blank check company (SEC SIC 6770).'),
  ('TEST_ISSUE',                 'EXCLUDED',   'Exchange test issue, not a real security.'),
  ('DELISTED',                   'EXCLUDED',   'No longer listed as of the evaluation date.'),
  ('NOT_COMMON_STOCK',           'EXCLUDED',   'Security type is not common stock and is out of scope.'),
  ('TYPE_UNKNOWN',               'UNRESOLVED', 'Security type could not be determined from available sources.'),
  ('ADR_ELIGIBILITY_UNDEFINED',  'UNRESOLVED', 'ADR found but the eligibility rule is not decided yet (D-10a).'),
  ('FOREIGN_STOCK_RULE_PENDING', 'UNRESOLVED', 'Foreign share listed on a target segment; treatment undecided (D-10b).'),
  ('INVESTMENT_CERTIFICATE_RULE_PENDING', 'UNRESOLVED', 'Investment certificate / preferred investment certificate; treatment undecided (D-10b).'),
  ('SEGMENT_RULE_PENDING',       'UNRESOLVED', 'Market segment exists but is not covered by the definition yet.'),
  ('REIT_DETECTION_INCOMPLETE',  'UNRESOLVED', 'Possible REIT that SIC-based detection could not confirm (D-10c).'),
  ('PROVIDER_DATA_MISSING',      'UNRESOLVED', 'Required provider data was missing or the fetch failed.')
on conflict (reason_code) do nothing;

create table universe.evaluations (
  evaluation_id        uuid primary key default gen_random_uuid(),
  run_id               uuid not null references pipeline.runs (run_id),
  universe_version     text not null references universe.definitions (universe_version),
  as_of_date           date not null,
  market_code          ref.market_code not null,
  security_id          uuid references ref.securities (security_id),
  listing_id           uuid references ref.listings (listing_id),
  decision             universe.decision not null,
  reason_code          text not null references universe.decision_reasons (reason_code),
  decision_detail      jsonb not null default '{}'::jsonb,
  evaluated_at         timestamptz not null,
  source_data_version  text not null,
  created_at           timestamptz not null default now(),
  unique (run_id, universe_version, as_of_date, listing_id)
);

comment on table universe.evaluations is 'One row per listing per run: included / excluded / unresolved with a reason and the source snapshot version used.';
comment on column universe.evaluations.source_data_version is 'Content hash of the provider snapshot the decision was made from.';

create index evaluations_lookup_idx on universe.evaluations (universe_version, as_of_date, market_code, decision);
create index evaluations_security_idx on universe.evaluations (security_id);

create table universe.coverage (
  coverage_id          uuid primary key default gen_random_uuid(),
  run_id               uuid not null references pipeline.runs (run_id),
  universe_version     text not null references universe.definitions (universe_version),
  as_of_date           date not null,
  market_code          ref.market_code not null,
  scope_kind           text not null,
  scope_value          text not null,
  expected_population  integer,
  expected_source      text,
  retrieved_count      integer not null,
  unique_count         integer not null,
  classified_count     integer not null,
  included_count       integer not null,
  excluded_count       integer not null,
  unresolved_count     integer not null,
  duplicate_count      integer not null default 0,
  provider_error_count integer not null default 0,
  computed_at          timestamptz not null,
  created_at           timestamptz not null default now(),
  constraint coverage_scope_kind_ck check (scope_kind in ('MARKET', 'EXCHANGE', 'MARKET_SEGMENT', 'SECURITY_TYPE')),
  unique (run_id, universe_version, as_of_date, market_code, scope_kind, scope_value)
);

comment on table universe.coverage is 'First-class coverage measurement. "All markets" is only a claim if these counts can be produced.';
