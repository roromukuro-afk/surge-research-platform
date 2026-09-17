-- The read contracts the web application is allowed to use.
--
-- A separate schema rather than direct table access, for three reasons that have
-- already bitten this project once each:
--
-- 1. **The as-of rules live in one place.** A screen that queries news.documents
--    directly can forget to filter on available_to_model_at. A screen that
--    queries ui.materials_recent cannot, because the filter is in the view.
-- 2. **Replay columns never reach a browser.** The counterfactual availability
--    time is research scaffolding; nothing here selects it.
-- 3. **The UI role reads and nothing else.** It is granted SELECT on this schema
--    and on nothing underneath it, so a mistake in the application cannot write.
--
-- These are contracts, so their column names are part of the interface: renaming
-- one is a breaking change and belongs in a new view, not an edit to this one.

create schema if not exists ui;
comment on schema ui is
  'Read-only contracts for the web application. The as-of rules and the column names are the interface.';

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market',
               'screening', 'news', 'material', 'chart', 'analysis', 'ui'];
$$;

alter default privileges in schema ui
  grant select on tables to surge_readonly, surge_worker_research;
alter default privileges in schema ui
  grant execute on functions to current_user;
alter default privileges in schema ui
  revoke execute on functions from public;

-- ---------------------------------------------------------------------------
-- Dashboard
-- ---------------------------------------------------------------------------

create or replace view ui.dashboard_daily as
  with technical as (
    select trade_date as as_of_date, market_code, count(*) as technical_candidates
    from screening.route_candidates group by 1, 2
  ),
  materials as (
    select as_of_date, market_code, count(*) as material_candidates
    from material.candidates group by 1, 2
  ),
  states as (
    select as_of_date,
           count(*) filter (where state = 'TECHNICAL_SETUP_EOD') as technical_setups,
           count(*) filter (where state = 'POST_CLOSE_CATALYST_SETUP') as catalyst_setups,
           -- Cast before matching: there is no LIKE operator on an enum.
           count(*) filter (where state::text like 'WATCH%') as watching,
           count(*) filter (where state = 'REJECT') as rejected,
           count(*) filter (where validation_status = 'REJECTED') as failed_validation,
           count(*) filter (where provider_kind = 'DETERMINISTIC_MOCK') as from_the_stand_in
    from analysis.stage3_outputs group by 1
  ),
  eligible as (
    select as_of_date, market_code,
           count(*) filter (where decision = 'PRICE_ELIGIBLE') as price_eligible,
           count(*) filter (where decision in ('STALE_PRICE', 'STALE_FX')) as stale
    from market.price_eligibility group by 1, 2
  )
  select
    coalesce(t.as_of_date, m.as_of_date, e.as_of_date) as as_of_date,
    coalesce(t.market_code, m.market_code, e.market_code) as market_code,
    coalesce(e.price_eligible, 0) as price_eligible,
    coalesce(e.stale, 0) as stale_inputs,
    coalesce(t.technical_candidates, 0) as technical_candidates,
    coalesce(m.material_candidates, 0) as material_candidates,
    coalesce(s.technical_setups, 0) as technical_setups,
    coalesce(s.catalyst_setups, 0) as catalyst_setups,
    coalesce(s.watching, 0) as watching,
    coalesce(s.rejected, 0) as rejected,
    coalesce(s.failed_validation, 0) as failed_validation,
    coalesce(s.from_the_stand_in, 0) as from_the_stand_in
  from technical t
  full outer join materials m on m.as_of_date = t.as_of_date and m.market_code = t.market_code
  full outer join eligible e on e.as_of_date = coalesce(t.as_of_date, m.as_of_date)
                            and e.market_code = coalesce(t.market_code, m.market_code)
  left join states s on s.as_of_date = coalesce(t.as_of_date, m.as_of_date, e.as_of_date);

comment on view ui.dashboard_daily is
  'One row per market per day. from_the_stand_in is on the dashboard rather than buried: a day whose verdicts all came from the deterministic mock has produced no analysis, and the screen should say so where it cannot be missed.';

-- ---------------------------------------------------------------------------
-- Universe
-- ---------------------------------------------------------------------------

create or replace view ui.universe_eligibility as
  select
    e.as_of_date,
    e.market_code,
    e.security_id,
    e.native_symbol,
    e.decision::text as price_decision,
    e.price,
    e.price_currency,
    e.converted_jpy as price_jpy,
    e.fx_rate,
    e.fx_age_seconds,
    e.price_age_days,
    e.rule_version,
    e.universe_decision::text as universe_decision,
    e.knowledge_cutoff
  from market.price_eligibility e;

comment on view ui.universe_eligibility is
  'Every security the 3,000 JPY rule was applied to, with the outcome AND the reason it could not be applied. STALE_PRICE and STALE_FX are outcomes in their own right, not a kind of failure to hide.';

-- ---------------------------------------------------------------------------
-- Materials
-- ---------------------------------------------------------------------------

create or replace function ui.materials_as_of(p_knowledge_cutoff timestamptz)
returns table (
  event_id uuid,
  event_key text,
  event_type text,
  headline text,
  scope text,
  first_known_at timestamptz,
  occurred_at timestamptz,
  occurred_at_precision text,
  independent_sources bigint,
  corroborations bigint,
  relevance text,
  relevance_reason text,
  securities bigint,
  strongest_relation text
)
language sql stable security invoker set search_path = pg_catalog, public as $$
  select
    e.event_id,
    e.event_key,
    e.event_type,
    e.headline,
    e.scope::text,
    e.first_known_at,
    e.occurred_at,
    e.occurred_at_precision::text,
    count(distinct s.source_key) filter (where s.source_role in ('DISCOVERY', 'VERIFICATION')),
    count(*) filter (where s.source_role = 'CORROBORATION'),
    max(d.relevance::text),
    max(d.reason_code),
    count(distinct r.security_id),
    min(r.relation_type::text)
  from material.events e
  left join material.event_sources s on s.event_id = e.event_id
  left join material.relevance_decisions d on d.event_id = e.event_id
  left join material.entity_relations r on r.event_id = e.event_id
  where e.first_known_at is not null and e.first_known_at <= p_knowledge_cutoff
  group by e.event_id, e.event_key, e.event_type, e.headline, e.scope,
           e.first_known_at, e.occurred_at, e.occurred_at_precision
  order by e.first_known_at desc;
$$;

comment on function ui.materials_as_of(timestamptz) is
  'Events the system could know by the cutoff. independent_sources counts distinct publishers in a discovering or verifying role and excludes corroboration, so twenty reprints of one wire story read as one source rather than twenty confirmations.';

-- ---------------------------------------------------------------------------
-- Stock detail
-- ---------------------------------------------------------------------------

create or replace function ui.stock_detail(p_security_id uuid, p_as_of_date date)
returns jsonb
language sql stable security invoker set search_path = pg_catalog, public as $$
  select jsonb_build_object(
    'security_id', p_security_id,
    'as_of_date', p_as_of_date,
    'eligibility', (
      select to_jsonb(x) from (
        select decision::text as decision, price, price_currency, converted_jpy,
               fx_age_seconds, price_age_days, rule_version
        from market.price_eligibility
        where security_id = p_security_id and as_of_date = p_as_of_date
        limit 1
      ) x
    ),
    'features', (
      select to_jsonb(f) from screening.features_daily f
      where f.security_id = p_security_id and f.trade_date = p_as_of_date
      limit 1
    ),
    'technical_routes', (
      select to_jsonb(discovery_routes) from screening.route_candidates
      where security_id = p_security_id and trade_date = p_as_of_date
      limit 1
    ),
    'material_routes', (
      select jsonb_build_object('routes', to_jsonb(discovery_routes), 'event_ids', to_jsonb(event_ids),
                                'strongest_relation', strongest_relation::text)
      from material.candidates
      where security_id = p_security_id and as_of_date = p_as_of_date
      limit 1
    ),
    'stage2', (
      select to_jsonb(s) from chart.stage2_assessments s
      where s.security_id = p_security_id and s.as_of_date = p_as_of_date
      limit 1
    ),
    -- Obstacles, explicitly labelled as such. The UI must not render these as
    -- targets: a prior high is where sellers wait (CLAUDE.md 1-11).
    'price_obstacles', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'kind', obstacle_kind::text, 'price_level', price_level,
               'distance_pct', distance_pct, 'established_on', established_on,
               'volume_at_level', volume_at_level, 'touch_count', touch_count,
               'weakening_evidence', weakening_evidence
             ) order by price_level), '[]'::jsonb)
      from chart.price_obstacles
      where security_id = p_security_id and as_of_date = p_as_of_date
    ),
    'stage3', (
      select to_jsonb(o) from analysis.stage3_outputs o
      where o.security_id = p_security_id and o.as_of_date = p_as_of_date
      limit 1
    )
  );
$$;

comment on function ui.stock_detail(uuid, date) is
  'Everything one screen needs about one security on one date, assembled server side so the client cannot assemble it wrongly. price_obstacles is named for what it is; rendering those levels as targets would invert the rule they exist to enforce.';

-- ---------------------------------------------------------------------------
-- Watch and setup
-- ---------------------------------------------------------------------------

create or replace view ui.watch_and_setup as
  select
    o.as_of_date,
    o.security_id,
    o.state::text as state,
    o.rationale,
    o.confidence_note,
    o.twenty_percent_threshold_price,
    o.threshold_reference_price,
    o.threshold_reference_kind,
    o.reachable_zone_low,
    o.reachable_zone_high,
    array(select unnest(o.reachable_zone_basis_kinds)::text) as reachable_zone_basis_kinds,
    o.reachable_zone_basis,
    o.obstacles_considered,
    o.concepts_considered,
    o.provider_kind::text as provider_kind,
    o.provider_id,
    o.validation_status::text as validation_status,
    o.validation_errors,
    o.knowledge_cutoff
  from analysis.stage3_outputs o
  where o.validation_status <> 'REJECTED';

comment on view ui.watch_and_setup is
  'Setups and watches that survived validation. The threshold and the reachable zone are separate columns because they are separate ideas: one is arithmetic on a reference price, the other is a judgement about plausible travel. No state here is an entry.';

-- ---------------------------------------------------------------------------
-- Coverage
-- ---------------------------------------------------------------------------

create or replace view ui.coverage_summary as
  select
    'news'::text as domain,
    c.as_of_date,
    c.source_key as component,
    c.items_listed::bigint as records,
    c.fetch_errors::bigint as provider_errors,
    c.quality_warnings::bigint as quality_warnings,
    c.parse_errors::bigint as our_parse_errors,
    c.coverage_quality::text as quality
  from news.source_coverage c
  union all
  select
    'market'::text,
    null::date,
    s.provider_id || '/' || s.dataset_key,
    count(*)::bigint,
    count(*) filter (where s.bar_count = 0)::bigint,
    count(*) filter (where s.corporate_action_completeness <> 'COMPLETE')::bigint,
    0::bigint,
    case
      when count(*) filter (where s.corporate_action_completeness = 'COMPLETE') = count(*) then 'COMPLETE'
      when count(*) filter (where s.corporate_action_completeness = 'NOT_PROVIDED') > 0 then 'NOT_PROVIDED'
      else 'PARTIAL_KNOWN_GAP'
    end
  from market.security_coverage s
  group by s.provider_id, s.dataset_key;

comment on view ui.coverage_summary is
  'Provider failures, our own parse errors and data quality warnings in separate columns. They are different problems - an outage, a bug of ours, and a provider that simply does not publish something - and summing them would hide which one is happening.';

create or replace view ui.unfilled_roles as
  select role::text as role, 'market data'::text as domain, 'no provider bound'::text as detail
  from market.unfilled_roles
  union all
  select 'NEWS_' || s.scope::text, 'news', 'enabled, but never fetched from the real service'
  from news.sources s
  where s.enabled = true and s.live_verified_at is null
  group by s.scope;

comment on view ui.unfilled_roles is
  'Roles the platform cannot fill today. Kept on a screen rather than in a report, because an unfilled role is invisible until someone asks why a number is zero.';

-- ---------------------------------------------------------------------------
-- Pipeline diagnostics
-- ---------------------------------------------------------------------------

create or replace view ui.pipeline_runs as
  select
    r.run_id,
    r.job_name,
    r.job_version,
    r.run_mode::text as run_mode,
    r.market_code::text as market_code,
    r.status::text as status,
    r.as_of_date,
    r.started_at,
    r.finished_at,
    r.data_cutoff,
    r.git_sha,
    r.config_hash,
    (p.run_id is not null) as published,
    p.published_at
  from pipeline.runs r
  left join pipeline.run_publications p on p.run_id = r.run_id;

comment on view ui.pipeline_runs is
  'Every run, and whether it was published. A run that finished is not a run whose results count: publication is what makes a run authoritative, and the two must stay visibly different.';

create or replace view ui.not_live_verified as
  select 'news source'::text as component, source_key as name,
         'adapter exists, never fetched from the real service'::text as detail
  from news.sources where live_verified_at is null
  union all
  select 'llm provider', provider_id,
         'adapter exists, never called the real model'
  from analysis.llm_providers where live_verified_at is null and provider_kind <> 'DETERMINISTIC_MOCK'
  union all
  select 'chart concept', concept_key,
         'enabled on synthetic examples only; no observed case either way'
  from chart.concept_evidence_grade where evidence_grade = 'SYNTHETIC_ONLY';

comment on view ui.not_live_verified is
  'Everything that is implemented and has never met real data. This is the IMPLEMENTED_NOT_LIVE_VERIFIED list, on a screen, so "it is built" never quietly becomes "it works".';

-- ---------------------------------------------------------------------------
-- Grants: read, and only read
-- ---------------------------------------------------------------------------

revoke all on all tables in schema ui from public;
grant usage on schema ui to surge_readonly, surge_worker_research;
grant select on all tables in schema ui to surge_readonly, surge_worker_research;
grant execute on function ui.materials_as_of(timestamptz) to surge_readonly, surge_worker_research;
grant execute on function ui.stock_detail(uuid, date) to surge_readonly, surge_worker_research;
