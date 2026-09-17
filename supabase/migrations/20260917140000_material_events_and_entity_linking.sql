-- Phase 5: material events, entity linking, and the union that feeds Stage 2.
--
-- Three separations carry this schema, and collapsing any of them would quietly
-- change what the platform can conclude:
--
-- 1. **Event, source and relation are different things.** Five articles about one
--    earnings revision are one event with five sources, not five materials. A
--    single event can touch a dozen securities through different mechanisms.
--
-- 2. **Discovery is not verification.** A source can be fast and often wrong, or
--    slow and authoritative. Which role a source played for a given event is a
--    property of the link, recorded per event, and there is deliberately no
--    global ranking that puts IR above news: material strength is measured from
--    the event's own features, not from the letterhead it arrived on.
--
-- 3. **The seven features stay seven.** novelty, surprise, directness, magnitude,
--    persistence, market reaction and priced-in are stored independently. A
--    single blended "strength" score would make it impossible to ask later which
--    of them actually predicted anything.

create schema if not exists material;
comment on schema material is
  'Material events: what happened, who says so, which securities it touches and through what mechanism.';

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market', 'screening', 'news', 'material'];
$$;

alter default privileges in schema material
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema material
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;
alter default privileges in schema material
  grant execute on functions to current_user;
alter default privileges in schema material
  revoke execute on functions from public;

-- ---------------------------------------------------------------------------
-- Vocabulary
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'relation_type' and n.nspname = 'material') then
    create type material.relation_type as enum (
      'DIRECT_COMPANY',
      'SUBSIDIARY',
      'PRODUCT',
      'CUSTOMER',
      'SUPPLIER',
      'COMPETITOR',
      'INDUSTRY',
      'POLICY_EXPOSURE',
      'COMMODITY_EXPOSURE',
      'FX_EXPOSURE',
      'RATE_EXPOSURE',
      'GEOPOLITICAL_EXPOSURE',
      'WEAK_ASSOCIATION'
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'source_role' and n.nspname = 'material') then
    create type material.source_role as enum ('DISCOVERY', 'VERIFICATION', 'CORROBORATION');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'market_relevance' and n.nspname = 'material') then
    create type material.market_relevance as enum ('RELEVANT', 'NOT_RELEVANT', 'UNCERTAIN');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'link_confidence' and n.nspname = 'material') then
    -- Same three-step vocabulary the security identity model uses, for the same
    -- reason: a link asserted from a registry identifier is not the same claim
    -- as one inferred from a sentence, and merging them loses the difference.
    create type material.link_confidence as enum ('STRONG', 'REGISTRY_ANCHORED', 'PROVISIONAL');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'candidate_origin' and n.nspname = 'material') then
    create type material.candidate_origin as enum ('TECHNICAL_ONLY', 'MATERIAL_ONLY', 'BOTH');
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Events
-- ---------------------------------------------------------------------------

create table if not exists material.events (
  event_id uuid primary key default gen_random_uuid(),
  event_key text not null,
  event_type text not null,
  headline text,
  scope news.source_scope not null,
  -- Best estimate of when the thing happened in the world. May be unknown.
  occurred_at timestamptz,
  occurred_at_precision news.time_precision not null default 'UNKNOWN',
  -- When THIS SYSTEM could first have known: the earliest available_to_model_at
  -- among the event's sources. Maintained by trigger so it cannot drift.
  first_known_at timestamptz,
  merge_version text not null,
  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),
  unique (event_key, merge_version)
);

comment on table material.events is
  'One row per real-world event, however many articles reported it. Merging is versioned: a change to the merge rule creates events under a new merge_version rather than rewriting the old ones.';
comment on column material.events.event_key is
  'The deduplication key. Two documents describing the same event must produce the same key, and the rule that produces it is versioned alongside it.';
comment on column material.events.first_known_at is
  'The earliest moment this system could have used the event: min(available_to_model_at) over its sources. Derived by trigger, never written by hand, because a hand-written knowledge time is a leak waiting to happen.';
comment on column material.events.occurred_at is
  'When it happened, as distinct from when we could know. Nullable on purpose: for many events the moment of occurrence is simply not published.';

create index if not exists events_first_known_idx on material.events (first_known_at);

-- ---------------------------------------------------------------------------
-- Sources: which document said so, and in what role
-- ---------------------------------------------------------------------------

create table if not exists material.event_sources (
  event_id uuid not null references material.events (event_id) on delete cascade,
  document_id uuid not null references news.documents (document_id) on delete cascade,
  source_role material.source_role not null,
  -- Denormalised from the document so as-of reads do not have to join, and so a
  -- later change to the document row cannot silently move the event's horizon.
  available_to_model_at timestamptz not null,
  source_key text not null references news.sources (source_key),
  match_evidence jsonb,
  linked_at timestamptz not null default clock_timestamp(),
  primary key (event_id, document_id)
);

comment on table material.event_sources is
  'Which documents report an event, and whether each one discovered it or confirmed it. A source may hold either role for different events; the role is not a property of the publisher.';
comment on column material.event_sources.source_role is
  'DISCOVERY found it first; VERIFICATION independently confirms it; CORROBORATION repeats it without adding independent weight. Recording the difference is what stops five copies of one wire story looking like five confirmations.';

create index if not exists event_sources_document_idx on material.event_sources (document_id);

-- A source may only play a role its registry entry permits.
create or replace function material.check_source_role() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
declare
  v_discovery boolean;
  v_verification boolean;
begin
  select discovery_role, verification_role into v_discovery, v_verification
  from news.sources where source_key = new.source_key;

  if not found then
    raise exception 'unknown news source %', new.source_key;
  end if;
  if new.source_role = 'DISCOVERY' and not v_discovery then
    raise exception 'source % is not registered for discovery', new.source_key;
  end if;
  if new.source_role = 'VERIFICATION' and not v_verification then
    raise exception 'source % is not registered for verification', new.source_key;
  end if;
  return new;
end;
$$;

drop trigger if exists material_event_sources_role_check on material.event_sources;
create trigger material_event_sources_role_check
  before insert on material.event_sources
  for each row execute function material.check_source_role();

-- first_known_at follows the sources, in both directions.
--
-- SECURITY DEFINER because no runtime role holds UPDATE on material.events, and
-- that is the point: the earliest moment we could have known an event is derived
-- from its sources and must not be settable by the code that writes the event.
create or replace function material.refresh_first_known_at() returns trigger
language plpgsql security definer set search_path = '' as $$
declare
  v_event uuid := coalesce(new.event_id, old.event_id);
begin
  update material.events e
     set first_known_at = (
       select min(s.available_to_model_at) from material.event_sources s where s.event_id = v_event
     )
   where e.event_id = v_event;
  return null;
end;
$$;

drop trigger if exists material_event_sources_knowledge on material.event_sources;
create trigger material_event_sources_knowledge
  after insert or delete on material.event_sources
  for each row execute function material.refresh_first_known_at();

-- ---------------------------------------------------------------------------
-- Relevance: the noise filter's verdict, with its reasoning
-- ---------------------------------------------------------------------------

create table if not exists material.relevance_decisions (
  event_id uuid not null references material.events (event_id) on delete cascade,
  ruleset_version text not null,
  relevance material.market_relevance not null,
  reason_code text not null,
  reason_detail text,
  signals jsonb,
  decided_at timestamptz not null default clock_timestamp(),
  primary key (event_id, ruleset_version)
);

comment on table material.relevance_decisions is
  'Why an event was kept or dropped. Keyword exclusion alone is forbidden (CLAUDE.md 1-13), so a decision carries a reason code and the signals behind it - enough for a later reader to disagree with it on the evidence.';
comment on column material.relevance_decisions.signals is
  'The measurements the verdict rested on. A NOT_RELEVANT with an empty signals object is a filter nobody can audit.';

-- ---------------------------------------------------------------------------
-- Entity relations: which securities, and through what mechanism
-- ---------------------------------------------------------------------------

create table if not exists material.entity_relations (
  relation_id bigint generated always as identity primary key,
  event_id uuid not null references material.events (event_id) on delete cascade,
  security_id uuid,
  issuer_id uuid,
  relation_type material.relation_type not null,
  confidence material.link_confidence not null,
  -- For anything that is not the company itself, HOW the event reaches the
  -- security. Macro exposure without a stated mechanism is astrology.
  causal_path text,
  evidence jsonb,
  extractor text not null,
  extractor_version text not null,
  created_at timestamptz not null default clock_timestamp(),

  constraint entity_relations_names_something check (security_id is not null or issuer_id is not null),
  constraint entity_relations_macro_needs_a_mechanism check (
    relation_type not in (
      'INDUSTRY', 'POLICY_EXPOSURE', 'COMMODITY_EXPOSURE',
      'FX_EXPOSURE', 'RATE_EXPOSURE', 'GEOPOLITICAL_EXPOSURE'
    )
    or (causal_path is not null and length(btrim(causal_path)) > 0)
  ),
  -- NULLS NOT DISTINCT because security_id and issuer_id are each nullable: with
  -- the default, two identical macro relations naming only an issuer would both
  -- be accepted, since NULL never equals NULL.
  unique nulls not distinct (event_id, security_id, issuer_id, relation_type, extractor_version)
);

comment on table material.entity_relations is
  'How an event reaches a security. relation_type is always stored (CLAUDE.md 1-13): "this article mentions the company" and "this policy raises the input cost of the company''s main product" are different claims and must not share a row shape.';
comment on column material.entity_relations.causal_path is
  'The mechanism, in words. Required for the six macro relation types by check constraint: a macro link with no stated path cannot be evaluated, defended or falsified.';
comment on column material.entity_relations.confidence is
  'How the link was established, not how strongly we feel about it. PROVISIONAL means text-derived and unconfirmed - it is a legitimate state to store, and an illegitimate one to hide.';

create index if not exists entity_relations_security_idx on material.entity_relations (security_id, relation_type);
create index if not exists entity_relations_event_idx on material.entity_relations (event_id);

-- ---------------------------------------------------------------------------
-- The seven features, kept apart
-- ---------------------------------------------------------------------------

create table if not exists material.event_security_features (
  event_id uuid not null references material.events (event_id) on delete cascade,
  security_id uuid not null,
  feature_version text not null,

  novelty numeric(6, 4),
  novelty_method text,
  surprise numeric(6, 4),
  surprise_method text,
  directness numeric(6, 4),
  directness_method text,
  magnitude numeric(6, 4),
  magnitude_method text,
  persistence numeric(6, 4),
  persistence_method text,
  market_reaction numeric(6, 4),
  market_reaction_method text,
  priced_in numeric(6, 4),
  priced_in_method text,

  evidence jsonb,
  knowledge_cutoff timestamptz not null,
  computed_at timestamptz not null default clock_timestamp(),
  primary key (event_id, security_id, feature_version),

  constraint features_in_unit_range check (
    coalesce(novelty, 0) between 0 and 1
    and coalesce(surprise, 0) between 0 and 1
    and coalesce(directness, 0) between 0 and 1
    and coalesce(magnitude, 0) between 0 and 1
    and coalesce(persistence, 0) between 0 and 1
    and coalesce(market_reaction, 0) between 0 and 1
    and coalesce(priced_in, 0) between 0 and 1
  )
);

comment on table material.event_security_features is
  'Seven independent measurements per (event, security). Deliberately not summed into a score: which of them carries predictive weight is a question for Phase 11, and blending them here would delete the evidence needed to answer it.';
comment on column material.event_security_features.market_reaction is
  'How the price actually responded - which is why these are per security rather than per event, and why knowledge_cutoff is stored: the reaction measurable at 15:00 is not the one measurable at the close.';
comment on column material.event_security_features.priced_in is
  'How much of the event the price appears to already reflect. Null means not measurable, which is different from zero.';
comment on column material.event_security_features.knowledge_cutoff is
  'The moment these were computed as of. A feature row without one cannot be replayed, because nobody can tell what it was allowed to see.';

-- ---------------------------------------------------------------------------
-- Material candidates, and the union with the technical side
-- ---------------------------------------------------------------------------

create table if not exists material.route_definitions (
  route_version text not null,
  route_code text not null,
  route_name text not null,
  description text not null,
  thresholds jsonb not null default '{}'::jsonb,
  requires_relation_types material.relation_type[] not null default '{}',
  enabled boolean not null default true,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  primary key (route_version, route_code)
);

comment on table material.route_definitions is
  'Material routes, the counterpart to screening Routes A-H. Like those, they are OR-type candidate generation: a security qualifies by firing any one of them.';
comment on column material.route_definitions.requires_relation_types is
  'Which relation types can satisfy this route. WEAK_ASSOCIATION appears in none of them, which is how CLAUDE.md 1-13 becomes testable rather than remembered.';

insert into material.route_definitions
  (route_version, route_code, route_name, description, thresholds, requires_relation_types)
values
  ('material-route-1.0.0', 'M1', 'Direct regulated disclosure',
   'The issuer''s own timely disclosure or statutory filing, about itself. The most direct material there is: no inference chain between the document and the security.',
   '{"min_novelty": 0.5, "document_types": ["TIMELY_DISCLOSURE", "STATUTORY_FILING"]}'::jsonb,
   '{DIRECT_COMPANY,SUBSIDIARY}'),
  ('material-route-1.0.0', 'M2', 'Independently verified company news',
   'A company-level event discovered by one source and confirmed by an independent one. Requires two DISTINCT sources in DISCOVERY and VERIFICATION roles; five copies of one wire story do not qualify.',
   '{"min_independent_sources": 2, "min_directness": 0.6}'::jsonb,
   '{DIRECT_COMPANY,SUBSIDIARY,PRODUCT}'),
  ('material-route-1.0.0', 'M3', 'Supply chain and counterparty exposure',
   'An event about someone else that reaches this issuer through a named commercial relationship. The causal path is mandatory at the schema level.',
   '{"min_directness": 0.4, "min_magnitude": 0.4}'::jsonb,
   '{CUSTOMER,SUPPLIER,COMPETITOR,PRODUCT}'),
  ('material-route-1.0.0', 'M4', 'Policy and rate exposure',
   'Government, regulator or central bank action with a stated transmission mechanism to this issuer.',
   '{"min_magnitude": 0.5, "requires_causal_path": true}'::jsonb,
   '{POLICY_EXPOSURE,RATE_EXPOSURE,INDUSTRY}'),
  ('material-route-1.0.0', 'M5', 'Commodity, FX and geopolitical exposure',
   'Input cost, currency or geopolitical shocks with a stated transmission mechanism. Held apart from M4 because the mechanisms differ and blending them would hide which one works.',
   '{"min_magnitude": 0.5, "requires_causal_path": true}'::jsonb,
   '{COMMODITY_EXPOSURE,FX_EXPOSURE,GEOPOLITICAL_EXPOSURE}'),
  ('material-route-1.0.0', 'M6', 'Surprise the price has not taken',
   'High surprise with low priced-in. Deliberately relation-agnostic apart from excluding WEAK_ASSOCIATION, because an unpriced surprise is interesting however it reaches the issuer.',
   '{"min_surprise": 0.7, "max_priced_in": 0.3}'::jsonb,
   '{DIRECT_COMPANY,SUBSIDIARY,PRODUCT,CUSTOMER,SUPPLIER,COMPETITOR,INDUSTRY,POLICY_EXPOSURE,COMMODITY_EXPOSURE,FX_EXPOSURE,RATE_EXPOSURE,GEOPOLITICAL_EXPOSURE}')
on conflict (route_version, route_code) do nothing;

-- Every route must exclude WEAK_ASSOCIATION, and the database says so rather
-- than trusting whoever adds the next route to remember.
alter table material.route_definitions
  drop constraint if exists route_definitions_no_weak_association_alone;
alter table material.route_definitions
  add constraint route_definitions_no_weak_association_alone
  check (not ('WEAK_ASSOCIATION' = any (requires_relation_types)));

create table if not exists material.candidates (
  run_id uuid not null references pipeline.runs (run_id),
  market_code ref.market_code not null,
  security_id uuid not null,
  as_of_date date not null,
  route_version text not null,
  feature_version text not null,
  discovery_routes text[] not null,
  route_count integer not null,
  event_ids uuid[] not null,
  route_evidence jsonb not null default '{}'::jsonb,
  strongest_relation material.relation_type,
  knowledge_cutoff timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',
  computed_at timestamptz not null default clock_timestamp(),
  primary key (run_id, security_id, as_of_date),

  constraint material_candidates_route_count check (cardinality(discovery_routes) = route_count),
  constraint material_candidates_fired_something check (route_count >= 1),
  constraint material_candidates_has_events check (cardinality(event_ids) >= 1)
);

comment on table material.candidates is
  'Securities the material side nominated, independently of the technical side. A security with no chart signal at all belongs here if an event reaches it, which is the point of running the two in parallel.';
comment on column material.candidates.strongest_relation is
  'The most direct relation type among the contributing events. WEAK_ASSOCIATION alone never makes a candidate (CLAUDE.md 1-13); it is recorded so that rule can be tested rather than trusted.';

-- The union Stage 2 reads.
--
-- A function rather than a view because the join has to be scoped to one run per
-- side. A view over the whole tables would join every technical run against
-- every material run for the same security and date, and quietly multiply the
-- candidate list by the number of times the pipeline had been re-run that day.
--
-- FULL OUTER on purpose: narrowing to securities both sides found would
-- reinstate exactly the AND filter this design exists to avoid.
create or replace function material.stage2_candidates(
  p_technical_run_id uuid,
  p_material_run_id uuid,
  p_as_of_date date
)
returns table (
  security_id uuid,
  as_of_date date,
  market_code ref.market_code,
  technical_routes text[],
  material_routes text[],
  event_ids uuid[],
  strongest_relation material.relation_type,
  origin material.candidate_origin,
  available_at timestamptz
)
language sql stable security invoker set search_path = pg_catalog, public as $$
  select
    coalesce(t.security_id, m.security_id),
    p_as_of_date,
    coalesce(t.market_code, m.market_code),
    coalesce(t.discovery_routes, '{}'::text[]),
    coalesce(m.discovery_routes, '{}'::text[]),
    m.event_ids,
    m.strongest_relation,
    -- Discriminate on run_id, not security_id. Both tables declare run_id NOT
    -- NULL, so it is the only column guaranteed to say which side of the outer
    -- join produced the row; a technical candidate whose security_id had not
    -- resolved would otherwise be labelled MATERIAL_ONLY.
    case
      when t.run_id is not null and m.run_id is not null then 'BOTH'::material.candidate_origin
      when t.run_id is not null then 'TECHNICAL_ONLY'::material.candidate_origin
      else 'MATERIAL_ONLY'::material.candidate_origin
    end,
    coalesce(t.available_at, m.available_at)
  from (
    select * from screening.route_candidates
    where run_id = p_technical_run_id and trade_date = p_as_of_date
  ) t
  full outer join (
    select * from material.candidates
    where run_id = p_material_run_id and as_of_date = p_as_of_date
  ) m on m.security_id = t.security_id;
$$;

comment on function material.stage2_candidates(uuid, uuid, date) is
  'What Stage 2 receives: the union of one technical run and one material run, with origin preserved. Scoped per run because an unscoped join would multiply candidates by the number of reruns that day.';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on all tables in schema material from public;
grant usage on schema material to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

grant select on all tables in schema material to surge_readonly, surge_purge;
grant select, insert on
  material.events, material.event_sources, material.relevance_decisions,
  material.entity_relations, material.event_security_features, material.candidates
to surge_worker_prod, surge_worker_research;
grant select on material.route_definitions to surge_worker_prod, surge_worker_research;
grant execute on function material.stage2_candidates(uuid, uuid, date)
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant usage on all sequences in schema material to surge_worker_prod, surge_worker_research;
