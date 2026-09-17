-- A principal for the web application, and only the contracts.
--
-- surge_readonly can read 79 base tables. That is right for a researcher at a
-- psql prompt and wrong for a web process: a page that reached past the contracts
-- would work, and the as-of filtering and the replay-column exclusion that live
-- in the views would be optional rather than enforced.
--
-- So the application connects as surge_web, which holds USAGE on the ui schema
-- and SELECT on its views and nothing else. Views run with their owner's rights
-- by default, so the contracts still read; the base tables underneath do not.
--
-- The two read-path functions are switched to SECURITY DEFINER for the same
-- reason. As invoker functions they would execute as surge_web and fail on the
-- tables they join, which would make them work for a researcher and break for
-- the application - the worst of both.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'surge_web') then
    -- NOLOGIN: the role is a privilege bundle. A login role for the deployment
    -- is granted this one and gets its password outside the repository, exactly
    -- as surge_worker_prod does.
    create role surge_web nologin;
  end if;
end;
$$;

comment on role surge_web is
  'The web application principal. Reads the ui contracts and nothing else: no base table, no write, no function outside ui.';

grant usage on schema ui to surge_web;
grant select on all tables in schema ui to surge_web;
alter default privileges in schema ui grant select on tables to surge_web;

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
language sql stable security definer set search_path = '' as $$
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
  'Events the system could know by the cutoff. independent_sources counts distinct publishers in a discovering or verifying role and excludes corroboration, so twenty reprints of one wire story read as one source rather than twenty confirmations. SECURITY DEFINER so the web principal can read the contract without being able to read the tables behind it.';

create or replace function ui.stock_detail(p_security_id uuid, p_as_of_date date)
returns jsonb
language sql stable security definer set search_path = '' as $$
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

-- These are the only functions the web principal may execute anywhere.
revoke all on function ui.materials_as_of(timestamptz) from public;
revoke all on function ui.stock_detail(uuid, date) from public;
grant execute on function ui.materials_as_of(timestamptz)
  to surge_web, surge_readonly, surge_worker_research;
grant execute on function ui.stock_detail(uuid, date)
  to surge_web, surge_readonly, surge_worker_research;
