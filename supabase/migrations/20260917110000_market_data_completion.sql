-- Phase 2: what a stored bar has to carry to be reconstructible.
--
-- security_id alone is not a recovery key. If an identity rebuild changes it -
-- and Phase 1 says a rebuild may - a series keyed only on security_id becomes
-- unreadable. Every bar therefore also carries what the PROVIDER called the
-- thing: its own security id where it has one, the native symbol, the exchange,
-- and the identity_version under which our security_id was assigned. Any one of
-- those is enough to find the row again.
--
-- The same reasoning adds source_data_version and source_timestamp: the first
-- says which snapshot of the provider's world this came from, the second is the
-- provider's own claim about when the value was struck, distinct from when we
-- read it.

alter table market.daily_bars
  add column if not exists provider_security_id text,
  add column if not exists source_data_version text,
  add column if not exists identity_version text,
  add column if not exists source_timestamp timestamptz;

comment on column market.daily_bars.provider_security_id is
  'The provider''s own permanent id for the security, where it has one. J-Quants has none (the 5-digit code is the identifier); EODHD has none either. Null is honest, and the native symbol plus exchange remains the fallback recovery key.';
comment on column market.daily_bars.identity_version is
  'Which identity ruleset assigned security_id. A rebuild changes security_id; this says which generation the row was written under so the old one can still be found.';
comment on column market.daily_bars.source_timestamp is
  'The provider''s own timestamp for the observation, when it publishes one. Distinct from observed_at (when we read it) and available_at (when we could act on it).';

alter table market.daily_bars_adjusted
  add column if not exists provider_security_id text,
  add column if not exists source_data_version text;

alter table market.corporate_actions
  add column if not exists provider_security_id text,
  add column if not exists source_data_version text,
  add column if not exists from_symbol text,
  add column if not exists to_symbol text;

comment on column market.corporate_actions.from_symbol is
  'For a ticker change: the symbol before. Kept on the action rather than only in the security master, because a price series keyed on the native symbol needs to know where it continues.';

alter table market.security_coverage
  add column if not exists provider_security_id text,
  add column if not exists bar_count integer,
  add column if not exists first_seen_at timestamptz,
  add column if not exists last_seen_at timestamptz;

-- TICKER_CHANGE was missing from the action vocabulary: EODHD publishes symbol
-- renames as their own feed and they adjust nothing about the price, but a
-- series that does not know about them silently splits in two.
do $$
begin
  if not exists (
    select 1 from pg_enum e join pg_type t on t.oid = e.enumtypid
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'market' and t.typname = 'corporate_action_type' and e.enumlabel = 'TICKER_CHANGE'
  ) then
    alter type market.corporate_action_type add value 'TICKER_CHANGE';
  end if;
end;
$$;

create index if not exists daily_bars_native_idx
  on market.daily_bars (provider_id, native_symbol, trade_date desc);
create index if not exists corporate_actions_type_idx
  on market.corporate_actions (action_type, ex_date);

-- ------------------------------------------------ market data run publication
-- A market data run is published the same way a universe run is: the result
-- stops being editable and downstream reads it through the publication rather
-- than by picking the newest run.
create or replace function market.latest_published_run(
  p_job_name text,
  p_market_code ref.market_code,
  p_knowledge_cutoff timestamptz default now()
)
returns uuid
language sql
stable
set search_path = ''
as $$
  select p.run_id
  from pipeline.run_publications p
  join pipeline.runs r on r.run_id = p.run_id
  where r.job_name = p_job_name
    and (p_market_code is null or r.market_code = p_market_code)
    and p.published_at <= p_knowledge_cutoff
  order by p.published_at desc, p.publication_seq desc
  limit 1;
$$;

comment on function market.latest_published_run(text, ref.market_code, timestamptz) is
  'The authoritative run of a market data job at a knowledge cutoff. Downstream never picks the newest run by finished_at: a run rebuilt today must not flow backwards into a past reading.';

grant execute on function market.latest_published_run(text, ref.market_code, timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;
