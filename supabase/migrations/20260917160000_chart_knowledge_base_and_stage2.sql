-- Phase 6: the chart knowledge base, and Stage 2.
--
-- The instruction for the knowledge base is "do not make it a dictionary of
-- pattern names", and that is enforced here rather than merely intended. A
-- concept cannot be stored without:
--
--   * a mechanism - why the thing would work, not just what it looks like
--   * a negative context - when it does not work
--   * a counterexample - something that looks like it and is not it
--   * numerical features - the measurable definition, which is the real record
--
-- and it cannot be ENABLED without at least one success example and at least one
-- FAILURE example. A concept with no recorded failures is not knowledge; it is a
-- name somebody liked.
--
-- Stage 2 stores measurements. The concepts that fired are recorded beside them,
-- but the numbers are the canonical record: a label can be re-derived from the
-- measurements, and measurements cannot be re-derived from a label.

create schema if not exists chart;
comment on schema chart is
  'The chart knowledge base and Stage 2 measurements. Concepts explain; numbers are the record.';

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array['ref', 'pipeline', 'universe', 'prod', 'research', 'market',
               'screening', 'news', 'material', 'chart'];
$$;

alter default privileges in schema chart
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema chart
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;
alter default privileges in schema chart
  grant execute on functions to current_user;
alter default privileges in schema chart
  revoke execute on functions from public;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'example_outcome' and n.nspname = 'chart') then
    create type chart.example_outcome as enum ('SUCCESS', 'FAILURE', 'COUNTEREXAMPLE');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'example_source' and n.nspname = 'chart') then
    -- SYNTHETIC examples are legitimate and must be labelled. A hand-built case
    -- that demonstrates a mechanism is useful teaching material and terrible
    -- evidence, and the column is what keeps the two apart.
    create type chart.example_source as enum ('SYNTHETIC', 'HISTORICAL_OBSERVED');
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'obstacle_kind' and n.nspname = 'chart') then
    create type chart.obstacle_kind as enum (
      'PRIOR_SURGE_HIGH',
      'SUPPLY_OVERHANG',
      'HORIZONTAL_RESISTANCE',
      'MOVING_AVERAGE',
      'ROUND_NUMBER',
      'GAP_EDGE',
      'VWAP_ANCHOR'
    );
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Concepts
-- ---------------------------------------------------------------------------

create table if not exists chart.concepts (
  concept_key text not null,
  concept_version text not null,
  name text not null,
  definition text not null,
  mechanism text not null,
  positive_context text not null,
  negative_context text not null,
  counterexample text not null,
  numerical_features text[] not null,
  enabled boolean not null default false,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  notes text,
  primary key (concept_key, concept_version),

  constraint concepts_prose_is_present check (
    length(btrim(definition)) > 20
    and length(btrim(mechanism)) > 20
    and length(btrim(positive_context)) > 10
    and length(btrim(negative_context)) > 10
    and length(btrim(counterexample)) > 10
  ),
  constraint concepts_are_measurable check (cardinality(numerical_features) >= 1)
);

comment on table chart.concepts is
  'One row per chart concept. The check constraints are the instruction "do not build a dictionary of pattern names" made enforceable: a concept without a mechanism, a negative context and a counterexample cannot be stored at all.';
comment on column chart.concepts.mechanism is
  'Why the thing would work - the behaviour of buyers and sellers it claims to describe. A concept whose mechanism is "it usually goes up after this" is curve fitting with a name on it.';
comment on column chart.concepts.negative_context is
  'When it fails. Required, because a concept that only ever describes its own successes cannot be wrong, and a thing that cannot be wrong cannot be tested.';
comment on column chart.concepts.counterexample is
  'Something that looks like this concept and is not it. This is what separates a concept from a shape.';
comment on column chart.concepts.numerical_features is
  'The measurable definition, naming columns in chart.stage2_assessments. These are the canonical record; the concept name is a handle for humans.';

create table if not exists chart.concept_examples (
  example_id bigint generated always as identity primary key,
  concept_key text not null,
  concept_version text not null,
  outcome chart.example_outcome not null,
  example_source chart.example_source not null,
  market_code ref.market_code,
  security_id uuid,
  as_of_date date,
  measurements jsonb not null,
  narrative text not null,
  what_happened_next text,
  created_at timestamptz not null default clock_timestamp(),

  constraint concept_examples_measured check (measurements <> '{}'::jsonb),
  constraint concept_examples_historical_names_a_security check (
    example_source <> 'HISTORICAL_OBSERVED'
    or (security_id is not null and as_of_date is not null)
  ),
  foreign key (concept_key, concept_version) references chart.concepts (concept_key, concept_version)
);

comment on table chart.concept_examples is
  'Worked cases per concept: what the numbers looked like and what happened next. Failures are not an afterthought here - a concept cannot be enabled without one.';
comment on column chart.concept_examples.measurements is
  'The numeric features at the time, so an example can be checked against the concept rather than believed.';

create index if not exists concept_examples_concept_idx
  on chart.concept_examples (concept_key, concept_version, outcome);

-- A concept may only be enabled once it has been shown to fail somewhere.
create or replace function chart.check_concept_has_failures() returns trigger
language plpgsql security invoker set search_path = pg_catalog, public as $$
declare
  v_success integer;
  v_failure integer;
begin
  if not new.enabled then
    return new;
  end if;

  select
    count(*) filter (where outcome = 'SUCCESS'),
    count(*) filter (where outcome in ('FAILURE', 'COUNTEREXAMPLE'))
  into v_success, v_failure
  from chart.concept_examples
  where concept_key = new.concept_key and concept_version = new.concept_version;

  if v_success < 1 or v_failure < 1 then
    raise exception
      'concept %/% cannot be enabled with % success and % failure examples. A concept with no recorded '
      'failures is a name, not knowledge',
      new.concept_key, new.concept_version, v_success, v_failure;
  end if;
  return new;
end;
$$;

drop trigger if exists chart_concepts_need_failures on chart.concepts;
create trigger chart_concepts_need_failures
  before insert or update on chart.concepts
  for each row execute function chart.check_concept_has_failures();

-- ---------------------------------------------------------------------------
-- Stage 2 measurements
-- ---------------------------------------------------------------------------

create table if not exists chart.stage2_assessments (
  run_id uuid not null references pipeline.runs (run_id),
  security_id uuid not null,
  as_of_date date not null,
  market_code ref.market_code not null,
  assessment_version text not null,
  series_basis screening.series_basis not null,

  -- price and trend
  close numeric(20, 6),
  prior_close numeric(20, 6),
  session_range_pct numeric(12, 6),
  close_position_in_range numeric(8, 6),
  gap_pct numeric(12, 6),

  -- volume and turnover
  volume bigint,
  relative_volume_20d numeric(12, 6),
  turnover numeric(24, 4),
  turnover_currency text,
  volume_trend_5d numeric(12, 6),
  up_volume_ratio_5d numeric(8, 6),

  -- support and resistance
  nearest_support numeric(20, 6),
  nearest_support_distance_pct numeric(12, 6),
  nearest_support_touches integer,
  nearest_resistance numeric(20, 6),
  nearest_resistance_distance_pct numeric(12, 6),
  nearest_resistance_touches integer,

  -- VWAP
  session_vwap numeric(20, 6),
  distance_from_session_vwap_pct numeric(12, 6),
  anchored_vwap numeric(20, 6),
  anchored_vwap_anchor_date date,
  distance_from_anchored_vwap_pct numeric(12, 6),

  -- volatility
  atr_14 numeric(20, 6),
  atr_pct numeric(12, 6),
  realized_vol_20d numeric(12, 6),
  volatility_expansion_ratio numeric(12, 6),

  -- seller exhaustion
  down_day_volume_decay numeric(12, 6),
  lower_wick_ratio numeric(8, 6),
  capitulation_volume_ratio numeric(12, 6),
  consecutive_down_days integer,

  -- failed breakout
  breakout_level numeric(20, 6),
  bars_above_breakout integer,
  closed_back_below boolean,
  failure_volume_ratio numeric(12, 6),

  -- healthy pullback
  pullback_depth_pct numeric(12, 6),
  pullback_volume_contraction numeric(12, 6),
  holding_above_ma20 boolean,
  pullback_bars integer,

  -- supply overhang
  overhead_volume_ratio numeric(12, 6),
  overhead_levels integer,
  distance_to_heaviest_overhead_pct numeric(12, 6),

  -- concepts, recorded beside the numbers rather than instead of them
  concepts_fired text[] not null default '{}',
  concept_evidence jsonb not null default '{}'::jsonb,
  concept_version text,

  -- provenance
  intraday_available boolean not null default false,
  measurement_gaps text[] not null default '{}',
  knowledge_cutoff timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null default 'OBSERVED_NOW',
  computed_at timestamptz not null default clock_timestamp(),

  primary key (run_id, security_id, as_of_date)
);

comment on table chart.stage2_assessments is
  'Stage 2 measurements for one security on one date. The numbers are the record; concepts_fired is a derived convenience. A label can be re-derived from measurements, and measurements cannot be re-derived from a label.';
comment on column chart.stage2_assessments.measurement_gaps is
  'Which measurements could not be taken, and implicitly why. A null column and a column we chose not to compute look identical without this.';
comment on column chart.stage2_assessments.intraday_available is
  'Whether minute data backed the session measurements. False means VWAP and the session figures came from daily bars, which is a coarser thing wearing the same column name.';
comment on column chart.stage2_assessments.turnover_currency is
  'Turnover is an amount in the security''s own currency, so a single cross-market threshold on it is meaningless. Stored per row for the same reason Route F splits its thresholds by market.';

create index if not exists stage2_security_idx on chart.stage2_assessments (security_id, as_of_date);

-- ---------------------------------------------------------------------------
-- Price obstacles
-- ---------------------------------------------------------------------------

create table if not exists chart.price_obstacles (
  obstacle_id bigint generated always as identity primary key,
  run_id uuid not null references pipeline.runs (run_id),
  security_id uuid not null,
  as_of_date date not null,
  obstacle_kind chart.obstacle_kind not null,
  price_level numeric(20, 6) not null,
  distance_pct numeric(12, 6),
  established_on date,
  volume_at_level numeric(24, 4),
  touch_count integer,
  strength_note text,
  weakening_evidence text,
  knowledge_cutoff timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),
  unique (run_id, security_id, as_of_date, obstacle_kind, price_level)
);

comment on table chart.price_obstacles is
  'Levels standing between the current price and higher ones. A prior surge high belongs here as an obstacle and NEVER as a source of upside (CLAUDE.md 1-11): the fact that a stock once traded at a price is not a reason it will again.';
comment on column chart.price_obstacles.weakening_evidence is
  'Why this obstacle may have lost force - new material, volume acceptance above it, a breakout that held. Recorded because the project forbids a rule that mechanically lowers the reachable zone for every old high: overhang expires, and the evidence that it has must be storable.';
comment on column chart.price_obstacles.established_on is
  'When the level was set. Older overhang is not automatically weaker, which is why this is a date to reason about rather than a decay factor applied for you.';

create index if not exists price_obstacles_security_idx on chart.price_obstacles (security_id, as_of_date);

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on all tables in schema chart from public;
grant usage on schema chart to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

grant select on all tables in schema chart to surge_readonly, surge_purge;
grant select on chart.concepts, chart.concept_examples to surge_worker_prod, surge_worker_research;
grant select, insert on chart.stage2_assessments, chart.price_obstacles
  to surge_worker_prod, surge_worker_research;
grant usage on all sequences in schema chart to surge_worker_prod, surge_worker_research;

-- ---------------------------------------------------------------------------
-- How good the evidence behind a concept actually is
-- ---------------------------------------------------------------------------

create or replace view chart.concept_evidence_grade as
  select
    c.concept_key,
    c.concept_version,
    c.enabled,
    count(*) filter (where e.outcome = 'SUCCESS' and e.example_source = 'HISTORICAL_OBSERVED') as observed_successes,
    count(*) filter (where e.outcome <> 'SUCCESS' and e.example_source = 'HISTORICAL_OBSERVED') as observed_failures,
    count(*) filter (where e.outcome = 'SUCCESS' and e.example_source = 'SYNTHETIC') as synthetic_successes,
    count(*) filter (where e.outcome <> 'SUCCESS' and e.example_source = 'SYNTHETIC') as synthetic_failures,
    case
      when count(*) filter (where e.example_source = 'HISTORICAL_OBSERVED' and e.outcome = 'SUCCESS') > 0
       and count(*) filter (where e.example_source = 'HISTORICAL_OBSERVED' and e.outcome <> 'SUCCESS') > 0
        then 'OBSERVED_BOTH_WAYS'
      when count(*) filter (where e.example_source = 'HISTORICAL_OBSERVED') > 0
        then 'OBSERVED_PARTIAL'
      else 'SYNTHETIC_ONLY'
    end as evidence_grade
  from chart.concepts c
  left join chart.concept_examples e
    on e.concept_key = c.concept_key and e.concept_version = c.concept_version
  group by c.concept_key, c.concept_version, c.enabled;

comment on view chart.concept_evidence_grade is
  'How much a concept has actually been tested. Every concept seeded before real price data exists grades SYNTHETIC_ONLY, and that must stay visible: a synthetic example demonstrates a mechanism, it does not evidence one. Promotion to OBSERVED_BOTH_WAYS happens when historical cases in both directions exist, not when someone is satisfied.';

grant select on chart.concept_evidence_grade
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- ---------------------------------------------------------------------------
-- Seed: six concepts, each with its mechanism and its failure mode.
--
-- Inserted disabled, given examples, then enabled - because the trigger refuses
-- to enable a concept that has never been shown to fail, and that check is the
-- point rather than an obstacle to route around.
-- ---------------------------------------------------------------------------

insert into chart.concepts
  (concept_key, concept_version, name, definition, mechanism, positive_context, negative_context,
   counterexample, numerical_features, enabled)
values
  ('SELLER_EXHAUSTION', 'chart-kb-1.0.0', 'Seller exhaustion',
   'A decline whose selling pressure is fading: each down day trades less than the last, and sessions close far above their lows.',
   'Forced and impatient sellers finish first. Once the supply that had to be sold has been sold, the same downward pressure no longer meets willing sellers, so price stops falling on equivalent effort. The long lower wick is the visible trace of that - buyers absorbing an intraday flush.',
   'After a sustained decline on high volume, in a security with no fresh negative material and a stable float.',
   'When the decline is driven by an ongoing information flow. Each new disclosure creates new sellers, so what looks like exhaustion is only the pause between waves.',
   'A quiet drift down on thin volume shows decaying volume without being exhaustion: nobody was exhausted because nobody was trying. Exhaustion needs the high-volume phase first.',
   '{down_day_volume_decay,lower_wick_ratio,capitulation_volume_ratio,consecutive_down_days}', false),

  ('FAILED_BREAKOUT', 'chart-kb-1.0.0', 'Failed breakout',
   'Price clears a level that mattered, fails to hold it, and closes back below within a few sessions.',
   'A breakout invites buyers who use the level itself as their reason. When it fails, those buyers are trapped above the market and become supply on any recovery, which is why a failed breakout leaves a security worse placed than one that never broke out at all.',
   'Where the level had several prior touches and the breakout came on weak volume relative to the range average.',
   'Where the failure is a single-session overshoot during a market-wide move; the level itself never truly changed hands.',
   'A breakout that trades below the level intraday but never closes below it has not failed - it has been tested. The distinction is the closing basis, which is why closed_back_below is stored apart from the level.',
   '{breakout_level,bars_above_breakout,closed_back_below,failure_volume_ratio}', false),

  ('HEALTHY_PULLBACK', 'chart-kb-1.0.0', 'Healthy pullback',
   'A retracement inside an advance where volume contracts as price eases and the trend structure stays intact.',
   'Contracting volume on the pullback says the sellers are profit-takers rather than new information acting. The people who want out are getting out against a thinner book, so the decline costs little effort and reverses cheaply.',
   'Within an established advance, with the retracement shallow relative to the prior leg and price holding a reference level such as the 20-day average.',
   'When the pullback volume matches or exceeds the advance volume. That is distribution wearing a pullback shape.',
   'A shallow decline in a security that was never advancing is not a pullback, it is noise. The concept requires the prior leg, which is why pullback_depth_pct is measured against that leg rather than against the recent range.',
   '{pullback_depth_pct,pullback_volume_contraction,holding_above_ma20,pullback_bars}', false),

  ('SUPPLY_OVERHANG', 'chart-kb-1.0.0', 'Supply overhang',
   'A band above the current price where a large amount of stock changed hands, leaving holders who are underwater and inclined to sell into a recovery.',
   'Holders who bought higher and held through a decline tend to sell at break-even. The more volume that traded in a band, the more such holders exist, so an advance into that band meets supply that was not present below it.',
   'Where heavy volume traded in a defined band and price has since fallen well below it, without a subsequent high-volume advance back through.',
   'Where new material has changed what the security is worth, or where price has already traded up through the band on heavy volume and held. The trapped holders have been relieved and the overhang has expired.',
   'A prior high with little volume behind it is a number on a chart, not an overhang. Volume at the level is what makes it supply, which is why volume_at_level is stored rather than the price alone.',
   '{overhead_volume_ratio,overhead_levels,distance_to_heaviest_overhead_pct}', false),

  ('VOLUME_DRY_UP', 'chart-kb-1.0.0', 'Volume dry-up',
   'A contraction in traded volume to well below its own recent average, while price moves in a narrowing range.',
   'Low volume in a narrow range means neither side will move price to transact. That balance is unstable: it resolves when one side becomes willing, and the move that follows tends to exceed the range that preceded it, because there is little resting interest to absorb it.',
   'After a directional move has stalled, in a security whose float is small enough that a modest change in interest moves price.',
   'In a security that is simply illiquid, where low volume is the permanent state rather than a contraction from anything.',
   'A holiday week produces the same measurements and says nothing about the security. Relative volume has to be measured against comparable sessions rather than against the calendar.',
   '{relative_volume_20d,volume_trend_5d,session_range_pct,atr_pct}', false),

  ('VWAP_RECLAIM', 'chart-kb-1.0.0', 'VWAP reclaim',
   'Price returning above the volume-weighted average price of a defined period, having traded below it.',
   'VWAP is the average price the period participants actually paid. Above it, the median position is profitable and holders are under no pressure; below it, the reverse. Reclaiming it moves a whole cohort from loss to break-even, which changes who is motivated to sell.',
   'Where the anchor is a meaningful event - an earnings release, a gap, the start of a decline - so the average price means something specific.',
   'Where the anchored period contains a single volume spike that dominates the average, making VWAP a proxy for one day rather than for a cohort.',
   'A session VWAP reclaim on a quiet day is arithmetic rather than information: with little volume the average sits close to the price and crossing it costs nothing. Anchoring matters, which is why the anchor date is stored alongside the level.',
   '{session_vwap,distance_from_session_vwap_pct,anchored_vwap,distance_from_anchored_vwap_pct,anchored_vwap_anchor_date}', false)
on conflict (concept_key, concept_version) do nothing;

insert into chart.concept_examples
  (concept_key, concept_version, outcome, example_source, measurements, narrative, what_happened_next)
values
  ('SELLER_EXHAUSTION', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"down_day_volume_decay": 0.42, "lower_wick_ratio": 0.61, "capitulation_volume_ratio": 3.8, "consecutive_down_days": 6}'::jsonb,
   'Six down sessions, the last trading 42% of the first session volume, closing in the top third of a wide range after a volume spike near four times average.',
   'Price stopped falling within two sessions and traded back into the prior range.'),
  ('SELLER_EXHAUSTION', 'chart-kb-1.0.0', 'FAILURE', 'SYNTHETIC',
   '{"down_day_volume_decay": 0.38, "lower_wick_ratio": 0.55, "capitulation_volume_ratio": 3.1, "consecutive_down_days": 5}'::jsonb,
   'Measurements almost identical to the success case, but a further disclosure arrived two sessions later.',
   'The decline resumed on fresh volume. The pattern was real and the mechanism was overtaken - which is exactly what the negative context predicts, and why the concept must never be read without it.'),

  ('FAILED_BREAKOUT', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"breakout_level": 1250.0, "bars_above_breakout": 2, "closed_back_below": true, "failure_volume_ratio": 1.9}'::jsonb,
   'Cleared a level touched four times before, held two sessions on below-average volume, then closed back below on nearly twice average volume.',
   'Traded down through the prior range over the following week as trapped buyers sold.'),
  ('FAILED_BREAKOUT', 'chart-kb-1.0.0', 'COUNTEREXAMPLE', 'SYNTHETIC',
   '{"breakout_level": 890.0, "bars_above_breakout": 1, "closed_back_below": false, "failure_volume_ratio": 0.8}'::jsonb,
   'Traded below the level intraday on the session after the breakout but closed above it.',
   'This is a test of the level, not a failure of it. Recorded so the closing-basis distinction has a case attached to it rather than only a sentence.'),

  ('HEALTHY_PULLBACK', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"pullback_depth_pct": 8.4, "pullback_volume_contraction": 0.45, "holding_above_ma20": true, "pullback_bars": 4}'::jsonb,
   'Four sessions easing 8.4% off the high on 45% of the advance volume, holding the 20-day average throughout.',
   'The advance resumed and exceeded the prior high.'),
  ('HEALTHY_PULLBACK', 'chart-kb-1.0.0', 'FAILURE', 'SYNTHETIC',
   '{"pullback_depth_pct": 7.9, "pullback_volume_contraction": 1.25, "holding_above_ma20": true, "pullback_bars": 4}'::jsonb,
   'Same depth, same duration, still above the 20-day average - but volume rose rather than contracted.',
   'Continued lower. The depth looked identical; the volume said it was distribution, which is why pullback_volume_contraction and not depth carries this concept.'),

  ('SUPPLY_OVERHANG', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"overhead_volume_ratio": 2.7, "overhead_levels": 2, "distance_to_heaviest_overhead_pct": 11.5}'::jsonb,
   'A band 11.5% above the price had absorbed 2.7 times the security normal volume during an earlier decline.',
   'The subsequent advance stalled on entry to the band and gave back most of the move.'),
  ('SUPPLY_OVERHANG', 'chart-kb-1.0.0', 'FAILURE', 'SYNTHETIC',
   '{"overhead_volume_ratio": 3.1, "overhead_levels": 2, "distance_to_heaviest_overhead_pct": 9.0}'::jsonb,
   'A heavier overhang, closer to the price, following a material event that changed the earnings base.',
   'Price advanced straight through on volume above the overhang volume itself. Overhang expires, which is why weakening_evidence exists on chart.price_obstacles and why no rule mechanically lowers a reachable zone for every old high.'),

  ('VOLUME_DRY_UP', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"relative_volume_20d": 0.38, "volume_trend_5d": -0.42, "session_range_pct": 1.6, "atr_pct": 2.1}'::jsonb,
   'Volume at 38% of its 20-day average with the daily range compressed to well under the ATR.',
   'Resolved with a move several times the compressed range.'),
  ('VOLUME_DRY_UP', 'chart-kb-1.0.0', 'COUNTEREXAMPLE', 'SYNTHETIC',
   '{"relative_volume_20d": 0.35, "volume_trend_5d": -0.40, "session_range_pct": 1.4, "atr_pct": 1.9}'::jsonb,
   'Indistinguishable measurements, in a security whose volume is always this thin.',
   'Nothing resolved, because nothing was compressed - this is the securitys normal state. The contraction has to be relative to the security own history, not to a market-wide figure.'),

  ('VWAP_RECLAIM', 'chart-kb-1.0.0', 'SUCCESS', 'SYNTHETIC',
   '{"session_vwap": 742.5, "distance_from_session_vwap_pct": 1.8, "anchored_vwap": 731.0, "distance_from_anchored_vwap_pct": 3.4, "anchored_vwap_anchor_date": "2026-08-12"}'::jsonb,
   'Reclaimed the VWAP anchored to an earnings gap, on volume above average.',
   'Held above the anchor and continued.'),
  ('VWAP_RECLAIM', 'chart-kb-1.0.0', 'FAILURE', 'SYNTHETIC',
   '{"session_vwap": 508.0, "distance_from_session_vwap_pct": 0.3, "anchored_vwap": 505.5, "distance_from_anchored_vwap_pct": 0.5, "anchored_vwap_anchor_date": "2026-08-12"}'::jsonb,
   'Crossed the session VWAP by 0.3% on a session trading a third of normal volume.',
   'Fell back the same day. On thin volume the average sits on top of the price and crossing it means nothing, which is the negative context made concrete.')
on conflict do nothing;

update chart.concepts
   set enabled = true
 where concept_version = 'chart-kb-1.0.0';
