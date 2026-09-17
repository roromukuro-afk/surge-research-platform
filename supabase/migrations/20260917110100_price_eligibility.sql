-- Phase 2: the 3,000 JPY hard filter, as a recorded decision rather than a WHERE clause.
--
-- The rule itself is one line - a Japanese share is eligible at or below 3,000
-- JPY, a US share at or below 3,000 JPY converted at the FX rate the system
-- could have known. Everything else here exists because the interesting cases
-- are the ones where the rule cannot be applied: no price, no rate, or a price
-- or rate old enough that applying it would be a guess dressed as a decision.
-- Those get their own outcomes and are never quietly folded into "not eligible".
--
-- Every row carries the three runs it depended on - the universe run, the market
-- data run and the FX run - and the version of the rule applied. A decision that
-- cannot be re-derived from what it names is not auditable, and this is the
-- decision every prediction in this project stands on.

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'price_eligibility_decision' and n.nspname = 'market') then
    create type market.price_eligibility_decision as enum (
      'PRICE_ELIGIBLE',    -- at or below the threshold on a price we could have known
      'PRICE_ABOVE_3000',  -- the rule applied and excluded it
      'PRICE_MISSING',     -- no price at all for this security at this cutoff
      'FX_MISSING',        -- a US security with no USD/JPY the cutoff could know
      'STALE_PRICE',       -- the newest price is older than the rule allows
      'STALE_FX'           -- the newest FX rate is older than the rule allows
    );
  end if;
end;
$$;

-- --------------------------------------------------------------- the rule
create table if not exists market.price_filter_rules (
  rule_version text primary key,
  threshold_jpy numeric(20, 4) not null,
  max_price_age_days integer not null,
  max_fx_age_seconds integer not null,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  notes text
);

comment on table market.price_filter_rules is
  'The eligibility rule, versioned. Changing a threshold or a staleness bound is a new rule_version, never an edit: decisions already recorded must keep meaning what they meant.';
comment on column market.price_filter_rules.max_price_age_days is
  'How old the price may be, in calendar days from the as-of date. A security that did not trade keeps its last close, and past this bound the last close stops being an answer and becomes STALE_PRICE.';
comment on column market.price_filter_rules.max_fx_age_seconds is
  'How old the FX observation may be. The ECB publishes only on TARGET days, so a long weekend legitimately produces a rate three days old; past this bound the conversion is refused rather than guessed.';

insert into market.price_filter_rules
  (rule_version, threshold_jpy, max_price_age_days, max_fx_age_seconds, notes)
values
  ('price-filter-1.0.0', 3000, 5, 345600,
   'The project''s standing 3,000 JPY limit. 5 calendar days covers a weekend plus a holiday; 345600 seconds is four days, which covers the longest ordinary TARGET closure. Both bounds are deliberately generous - their job is to catch a feed that has stopped, not to second-guess a quiet security.')
on conflict (rule_version) do nothing;

-- --------------------------------------------------------- the decisions
create table if not exists market.price_eligibility (
  run_id uuid not null references pipeline.runs (run_id),
  market_code ref.market_code not null,
  provider_id text not null,
  native_symbol text not null,
  as_of_date date not null,

  security_id uuid references ref.securities (security_id),
  exchange_code text,
  identity_version text,
  universe_decision universe.decision,

  decision market.price_eligibility_decision not null,
  rule_version text not null references market.price_filter_rules (rule_version),

  price numeric(20, 6),
  price_currency text,
  price_basis market.price_basis,
  price_trade_date date,
  price_observed_at timestamptz,
  price_available_at timestamptz,
  price_age_days integer,

  fx_rate numeric(20, 10),
  fx_source_date date,
  fx_observed_at timestamptz,
  fx_available_at timestamptz,
  fx_age_seconds numeric,

  converted_jpy numeric(20, 6),

  universe_run_id uuid references pipeline.runs (run_id),
  market_data_run_id uuid references pipeline.runs (run_id),
  fx_run_id uuid references pipeline.runs (run_id),

  knowledge_cutoff timestamptz not null,
  computed_at timestamptz not null default clock_timestamp(),

  primary key (run_id, provider_id, market_code, native_symbol),
  constraint price_eligibility_eligible_needs_price_ck check (
    decision <> 'PRICE_ELIGIBLE' or (price is not null and converted_jpy is not null)
  ),
  constraint price_eligibility_jp_needs_no_fx_ck check (
    market_code <> 'JP' or decision not in ('FX_MISSING', 'STALE_FX')
  )
);

comment on table market.price_eligibility is
  'One row per security per run: what the 3,000 JPY rule decided, on which price and which rate, and which three runs those came from. The failure outcomes are first-class - a security with no price is not the same as one that is too expensive, and a research set that conflates them is measuring its own gaps.';
comment on column market.price_eligibility.universe_decision is
  'What the universe run said about this security. UNRESOLVED securities are priced and stored - they are still evidence - but they are not part of the prediction universe; that is what prediction_eligible encodes.';
comment on column market.price_eligibility.converted_jpy is
  'price x fx_rate for US, price itself for JP. Stored rather than recomputed so the decision can be checked against the numbers that produced it.';

create index if not exists price_eligibility_run_decision_idx
  on market.price_eligibility (run_id, decision);
create index if not exists price_eligibility_security_idx
  on market.price_eligibility (security_id, as_of_date);
create index if not exists price_eligibility_asof_idx
  on market.price_eligibility (market_code, as_of_date);

-- A published eligibility run is a result other things depend on, so it freezes
-- exactly like a universe run does.
drop trigger if exists freeze_published_price_eligibility on market.price_eligibility;
create trigger freeze_published_price_eligibility
  before insert or update or delete on market.price_eligibility
  for each row execute function pipeline.freeze_published_run_artifacts();

-- ----------------------------------------------------------------- readers
create or replace function market.price_eligibility_as_of(
  p_market_code ref.market_code,
  p_as_of_date date,
  p_knowledge_cutoff timestamptz default now()
)
returns setof market.price_eligibility
language sql
stable
set search_path = ''
as $$
  select e.*
  from market.price_eligibility e
  where e.run_id = (
    select p.run_id
    from pipeline.run_publications p
    join pipeline.runs r on r.run_id = p.run_id
    where r.job_name = 'price_eligibility'
      and r.market_code = p_market_code
      and r.as_of_date = p_as_of_date
      and p.published_at <= p_knowledge_cutoff
    order by p.published_at desc, p.publication_seq desc
    limit 1
  );
$$;

comment on function market.price_eligibility_as_of(ref.market_code, date, timestamptz) is
  'The eligibility decisions in force for a trading date, as of a knowledge cutoff. Reads through the publication, so a run rebuilt later cannot flow backwards into an earlier reading.';

create or replace view market.prediction_universe as
  select e.*
  from market.price_eligibility e
  where e.decision = 'PRICE_ELIGIBLE'
    and e.universe_decision = 'INCLUDED';

comment on view market.prediction_universe is
  'What a formal prediction may be made about: eligible on price, and resolved as INCLUDED by the universe. UNRESOLVED securities are deliberately absent here while remaining present in price_eligibility.';

create or replace view market.eligibility_coverage as
  select run_id,
         market_code,
         as_of_date,
         count(*) as evaluated,
         count(*) filter (where decision = 'PRICE_ELIGIBLE') as eligible,
         count(*) filter (where decision = 'PRICE_ABOVE_3000') as above_threshold,
         count(*) filter (where decision = 'PRICE_MISSING') as price_missing,
         count(*) filter (where decision = 'FX_MISSING') as fx_missing,
         count(*) filter (where decision = 'STALE_PRICE') as stale_price,
         count(*) filter (where decision = 'STALE_FX') as stale_fx,
         count(*) filter (where universe_decision = 'UNRESOLVED') as unresolved_identity,
         count(*) filter (where decision = 'PRICE_ELIGIBLE' and universe_decision = 'INCLUDED')
           as prediction_universe
  from market.price_eligibility
  group by run_id, market_code, as_of_date;

comment on view market.eligibility_coverage is
  'Per run: how many securities the rule could and could not be applied to, broken out by why. A rise in price_missing is a pipeline failure, not a market event, and it should be visible as one.';

-- ---------------------------------------------------------------- privileges
revoke all on table market.price_eligibility, market.price_filter_rules from public;
grant select on table market.price_filter_rules, market.price_eligibility
  to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;
grant insert on table market.price_eligibility to surge_worker_prod;
grant select on market.prediction_universe, market.eligibility_coverage
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant delete on table market.price_eligibility to surge_purge;

grant execute on function market.price_eligibility_as_of(ref.market_code, date, timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;
