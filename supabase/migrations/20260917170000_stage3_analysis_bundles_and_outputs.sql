-- Phase 7: Stage 3 EOD analysis.
--
-- Three rules shape this schema, and each is enforced rather than remembered.
--
-- **No ENTRY here.** The output states stop at setup and watch. A formal entry
-- prediction is a Phase 8 decision made during the session against a live price,
-- and an end-of-day analysis has no business producing one. The enum simply has
-- no ENTRY member, so the mistake is unrepresentable.
--
-- **The 20% threshold and the reachable zone are different things.** The
-- threshold is arithmetic on the entry reference price. The reachable zone is a
-- judgement about how far the price could plausibly travel. Storing them in one
-- column would let a target be justified by a target.
--
-- **A prior high is never a reason to expect a rise.** reachable_zone_basis_kinds
-- is a closed set that does not contain PRIOR_HIGH, and a check constraint
-- requires at least one of the permitted kinds. Old highs live in
-- chart.price_obstacles, where they belong.

create schema if not exists analysis;
comment on schema analysis is
  'Stage 3: the input bundle handed to a model, what came back, and whether it survived validation.';

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market',
               'screening', 'news', 'material', 'chart', 'analysis'];
$$;

alter default privileges in schema analysis
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema analysis
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;
alter default privileges in schema analysis
  grant execute on functions to current_user;
alter default privileges in schema analysis
  revoke execute on functions from public;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'stage3_state' and n.nspname = 'analysis') then
    -- No ENTRY. Deliberately. See the header.
    create type analysis.stage3_state as enum (
      'TECHNICAL_SETUP_EOD',
      'POST_CLOSE_CATALYST_SETUP',
      'WATCH_BREAKOUT',
      'WATCH_PULLBACK',
      'WATCH_OTHER',
      'REJECT'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'zone_basis_kind' and n.nspname = 'analysis') then
    -- A closed set, and PRIOR_HIGH is not in it.
    create type analysis.zone_basis_kind as enum (
      'CURRENT_MATERIAL',
      'SUPPLY_STRUCTURE',
      'VOLUME_STRUCTURE',
      'SUPPORT_RESISTANCE',
      'VOLATILITY_RANGE',
      'SECTOR_MOVE'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'validation_status' and n.nspname = 'analysis') then
    create type analysis.validation_status as enum ('PASSED', 'REPAIRED', 'REJECTED');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'provider_kind' and n.nspname = 'analysis') then
    create type analysis.provider_kind as enum ('DETERMINISTIC_MOCK', 'HOSTED_LLM', 'LOCAL_LLM');
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- The input bundle
-- ---------------------------------------------------------------------------

create table if not exists analysis.input_bundles (
  bundle_id uuid primary key default gen_random_uuid(),
  run_id uuid not null references pipeline.runs (run_id),
  security_id uuid not null,
  as_of_date date not null,
  market_code ref.market_code not null,
  bundle_version text not null,

  -- The canonical prompt and its addenda travel as hashes, in separate columns
  -- and never concatenated. Mixing v5.1 with post-v5.1 decisions is forbidden by
  -- the project rules, and one combined hash would make a violation invisible.
  canonical_prompt_sha256 text not null,
  addenda_sha256 text[] not null default '{}',

  -- What each section was built from. Versions, not payloads: the payload lives
  -- in object storage, and the row has to stay readable.
  feature_version text,
  route_version text,
  material_route_version text,
  material_feature_version text,
  stage2_version text,
  concept_version text,
  relevance_ruleset_version text,
  merge_version text,

  section_digests jsonb not null,
  bundle_sha256 text not null,
  bundle_object_key text,

  knowledge_cutoff timestamptz not null,
  built_at timestamptz not null default clock_timestamp(),

  constraint input_bundles_canonical_hash check (canonical_prompt_sha256 ~ '^[0-9a-f]{64}$'),
  constraint input_bundles_bundle_hash check (bundle_sha256 ~ '^[0-9a-f]{64}$'),
  constraint input_bundles_has_sections check (section_digests <> '{}'::jsonb),
  unique (run_id, security_id, as_of_date)
);

comment on table analysis.input_bundles is
  'Exactly what a model was given, recorded so the analysis can be reproduced. The payload lives in object storage; this row holds the hashes and the versions of every component that went in.';
comment on column analysis.input_bundles.canonical_prompt_sha256 is
  'The hash of the immutable v5.1 canonical prompt. Kept apart from the addenda hashes because the project forbids mixing the canonical text with later decisions - one combined hash would hide exactly that.';
comment on column analysis.input_bundles.section_digests is
  'Per-section hash: market data, features, routes, materials, entity links, obstacles, coverage. A section that changed between two runs is identifiable without diffing megabytes.';
comment on column analysis.input_bundles.knowledge_cutoff is
  'What the bundle was allowed to see. Every section is built through an as-of read, and this is the argument they were all given.';

-- ---------------------------------------------------------------------------
-- The output
-- ---------------------------------------------------------------------------

create table if not exists analysis.stage3_outputs (
  output_id uuid primary key default gen_random_uuid(),
  bundle_id uuid not null references analysis.input_bundles (bundle_id),
  run_id uuid not null references pipeline.runs (run_id),
  security_id uuid not null,
  as_of_date date not null,

  state analysis.stage3_state not null,
  rationale text not null,
  confidence_note text,

  -- Arithmetic on a reference price. Not a judgement, not a target.
  twenty_percent_threshold_price numeric(20, 6),
  threshold_reference_price numeric(20, 6),
  threshold_reference_kind text,

  -- A judgement about plausible travel, built from current structure.
  reachable_zone_low numeric(20, 6),
  reachable_zone_high numeric(20, 6),
  reachable_zone_basis_kinds analysis.zone_basis_kind[] not null default '{}',
  reachable_zone_basis text,

  obstacles_considered jsonb not null default '[]'::jsonb,
  materials_considered jsonb not null default '[]'::jsonb,
  concepts_considered text[] not null default '{}',

  provider_kind analysis.provider_kind not null,
  provider_id text not null,
  model_id text,
  model_parameters jsonb not null default '{}'::jsonb,
  prompt_sha256 text not null,
  response_sha256 text not null,

  validation_status analysis.validation_status not null,
  validation_errors text[] not null default '{}',

  knowledge_cutoff timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',
  created_at timestamptz not null default clock_timestamp(),

  constraint stage3_prompt_hash check (prompt_sha256 ~ '^[0-9a-f]{64}$'),
  constraint stage3_response_hash check (response_sha256 ~ '^[0-9a-f]{64}$'),
  constraint stage3_zone_is_ordered check (
    reachable_zone_low is null or reachable_zone_high is null or reachable_zone_high >= reachable_zone_low
  ),
  -- A zone without a permitted basis is not a zone, it is a wish. And because
  -- PRIOR_HIGH is not a member of the enum, "it used to trade there" cannot be
  -- the reason even by accident.
  constraint stage3_zone_needs_a_permitted_basis check (
    reachable_zone_high is null
    or (cardinality(reachable_zone_basis_kinds) >= 1
        and reachable_zone_basis is not null
        and length(btrim(reachable_zone_basis)) > 0)
  ),
  constraint stage3_rejected_says_why check (
    validation_status <> 'REJECTED' or cardinality(validation_errors) >= 1
  ),
  unique (run_id, security_id, as_of_date)
);

comment on table analysis.stage3_outputs is
  'What the model concluded, and everything needed to reproduce it. Append-only: a revised view is a new run, never an edit.';
comment on column analysis.stage3_outputs.state is
  'Setup or watch or reject. There is no ENTRY member: a formal entry prediction is a Phase 8 decision made during the session against a live price, and an end-of-day analysis cannot make one.';
comment on column analysis.stage3_outputs.twenty_percent_threshold_price is
  'Arithmetic on threshold_reference_price. Held apart from the reachable zone so a target can never be justified by a target.';
comment on column analysis.stage3_outputs.reachable_zone_basis_kinds is
  'Why the zone is where it is, from a closed set that does not include prior highs (CLAUDE.md 1-11). Old highs are obstacles and live in chart.price_obstacles.';
comment on column analysis.stage3_outputs.obstacles_considered is
  'The levels in the way, carried into the analysis so a zone drawn straight through heavy overhang is visible as such rather than merely optimistic.';
comment on column analysis.stage3_outputs.prompt_sha256 is
  'Hash of the exact text sent. Required by CLAUDE.md 1-18: an LLM input that was not hashed cannot be audited later.';

create index if not exists stage3_outputs_state_idx on analysis.stage3_outputs (as_of_date, state);
create index if not exists stage3_outputs_security_idx on analysis.stage3_outputs (security_id, as_of_date);

create or replace function analysis.forbid_output_mutation() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
begin
  raise exception
    'analysis.stage3_outputs is append-only; supersede output % with a new run rather than editing it',
    coalesce(new.output_id, old.output_id);
end;
$$;

drop trigger if exists analysis_stage3_outputs_append_only on analysis.stage3_outputs;
create trigger analysis_stage3_outputs_append_only
  before update or delete on analysis.stage3_outputs
  for each row execute function analysis.forbid_output_mutation();

-- ---------------------------------------------------------------------------
-- Provider registry: no paid model is required to run the pipeline
-- ---------------------------------------------------------------------------

create table if not exists analysis.llm_providers (
  provider_id text primary key,
  provider_kind analysis.provider_kind not null,
  name text not null,
  model_id text,
  required_env text[] not null default '{}',
  monthly_cost_jpy integer not null default 0,
  enabled boolean not null default false,
  live_verified_at timestamptz,
  notes text
);

comment on table analysis.llm_providers is
  'Which models Stage 3 may use. The deterministic mock is the default and needs no credential, so the pipeline runs end to end at zero recurring cost; a hosted model is an upgrade, not a prerequisite.';
comment on column analysis.llm_providers.live_verified_at is
  'Null means IMPLEMENTED_NOT_LIVE_VERIFIED: the adapter exists and is tested, and has never spoken to the real service.';

insert into analysis.llm_providers
  (provider_id, provider_kind, name, model_id, required_env, monthly_cost_jpy, enabled, notes)
values
  ('deterministic_mock', 'DETERMINISTIC_MOCK', 'Deterministic rule-based stand-in', null, '{}', 0, true,
   'Produces a state and a rationale from the bundle by fixed rules. Reproducible, free, and honest about being a stand-in: it is not a judgement, it is a pipeline under test.')
on conflict (provider_id) do nothing;

create or replace view analysis.stage3_state_counts as
  select as_of_date, state, validation_status, count(*) as outputs
  from analysis.stage3_outputs
  group by as_of_date, state, validation_status
  order by as_of_date desc, state;

comment on view analysis.stage3_state_counts is
  'How many securities landed in each state, split by whether the output survived validation. A day where everything passed validation is as worth noticing as one where nothing did.';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on all tables in schema analysis from public;
grant usage on schema analysis to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

grant select on all tables in schema analysis to surge_readonly, surge_purge;
grant select, insert on analysis.input_bundles, analysis.stage3_outputs
  to surge_worker_prod, surge_worker_research;
grant select on analysis.llm_providers to surge_worker_prod, surge_worker_research;
grant select on analysis.stage3_state_counts
  to surge_worker_prod, surge_worker_research, surge_readonly;
