-- Phase 1.1b: knowledge time and effective time are not the same thing.
--
-- The as-of readers filtered on available_at <= cutoff and effective_to > cutoff
-- but never on effective_from <= cutoff. A row that was KNOWN at T1 and becomes
-- EFFECTIVE at T2 ("from Monday the ticker is XYZ") was therefore returned for a
-- cutoff of T1, which is the future leaking into the past.
--
-- Both dimensions are now explicit:
--   * available_at  - when the system could know the row (knowledge time)
--   * effective_from / effective_to - when the fact itself holds (valid time)
--
-- The one argument form answers "as it was, as known then" by using the same
-- instant for both, which is what every current caller means.

-- ------------------------------------------------------------- tickers as-of
create or replace function ref.listing_symbols_as_of(
  p_effective_at timestamptz,
  p_known_at timestamptz
)
returns table (
  listing_id      uuid,
  symbol          text,
  symbol_type     text,
  effective_from  timestamptz,
  effective_to    timestamptz
)
language sql
stable
set search_path = ''
as $$
  select ls.listing_id, ls.symbol, ls.symbol_type, ls.effective_from, ls.effective_to
  from ref.listing_symbols ls
  where ls.available_at <= p_known_at
    and ls.effective_from <= p_effective_at
    and (ls.effective_to is null or ls.effective_to > p_effective_at);
$$;

create or replace function ref.listing_symbols_as_of(p_available_at timestamptz)
returns table (
  listing_id      uuid,
  symbol          text,
  symbol_type     text,
  effective_from  timestamptz,
  effective_to    timestamptz
)
language sql
stable
set search_path = ''
as $$
  select * from ref.listing_symbols_as_of(p_available_at, p_available_at);
$$;

comment on function ref.listing_symbols_as_of(timestamptz, timestamptz) is
  'Tickers by valid time and knowledge time: what was true at p_effective_at, using only what was knowable by p_known_at.';
comment on function ref.listing_symbols_as_of(timestamptz) is
  'Tickers as they were, as known then. Shorthand for the two argument form with one instant.';

-- ------------------------------------------------------------ listings as-of
create or replace function ref.listings_as_of(
  p_effective_at timestamptz,
  p_known_at timestamptz
)
returns table (
  listing_id           uuid,
  security_id          uuid,
  exchange_id          text,
  symbol               text,
  market_segment_code  text,
  listing_status       ref.listing_status,
  effective_from       timestamptz,
  effective_to         timestamptz,
  symbol_effective_from timestamptz
)
language sql
stable
set search_path = ''
as $$
  select l.listing_id,
         l.security_id,
         l.exchange_id,
         sym.symbol,
         st.market_segment_code,
         st.listing_status,
         st.effective_from,
         st.effective_to,
         sym.effective_from
  from ref.listings l
  join ref.listing_states st
    on st.listing_id = l.listing_id
   and st.available_at <= p_known_at
   and st.effective_from <= p_effective_at
   and (st.effective_to is null or st.effective_to > p_effective_at)
  left join lateral (
    -- direct read so the index on (listing_id, ...) is usable
    select ls.symbol, ls.effective_from
    from ref.listing_symbols ls
    where ls.listing_id = l.listing_id
      and ls.symbol_type = 'TICKER'
      and ls.available_at <= p_known_at
      and ls.effective_from <= p_effective_at
      and (ls.effective_to is null or ls.effective_to > p_effective_at)
    order by ls.effective_from desc
    limit 1
  ) sym on true;
$$;

create or replace function ref.listings_as_of(p_available_at timestamptz)
returns table (
  listing_id           uuid,
  security_id          uuid,
  exchange_id          text,
  symbol               text,
  market_segment_code  text,
  listing_status       ref.listing_status,
  effective_from       timestamptz,
  effective_to         timestamptz,
  symbol_effective_from timestamptz
)
language sql
stable
set search_path = ''
as $$
  select * from ref.listings_as_of(p_available_at, p_available_at);
$$;

comment on function ref.listings_as_of(timestamptz, timestamptz) is
  'The listing master by valid time and knowledge time. Attributes from ref.listing_states and the ticker from ref.listing_symbols, both filtered on effective_from <= p_effective_at and available_at <= p_known_at. Nothing is read from the materialised current row.';
comment on function ref.listings_as_of(timestamptz) is
  'The listing master as it was, as known then. Shorthand for the two argument form with one instant.';

grant execute on function
  ref.listings_as_of(timestamptz),
  ref.listings_as_of(timestamptz, timestamptz),
  ref.listing_symbols_as_of(timestamptz),
  ref.listing_symbols_as_of(timestamptz, timestamptz)
to surge_worker_prod, surge_worker_research, surge_readonly;
