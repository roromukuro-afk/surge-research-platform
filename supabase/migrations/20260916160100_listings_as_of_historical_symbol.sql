-- Phase 1.1a: as-of reads return the ticker that was current at the cutoff.
--
-- ref.listings_as_of read its attributes from ref.listing_states (versioned) but
-- its code from ref.listings.local_code, which is the materialised *current*
-- state. For a US listing local_code is the ticker, so an as-of read of T1 could
-- return T1's segment next to T2's ticker.
--
-- ref.listings.local_code stays as the current materialised value and is never
-- the source of truth for a historical ticker; ref.listing_symbols is.

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
  select ls.listing_id, ls.symbol, ls.symbol_type, ls.effective_from, ls.effective_to
  from ref.listing_symbols ls
  where ls.available_at <= p_available_at
    and (ls.effective_to is null or ls.effective_to > p_available_at);
$$;

comment on function ref.listing_symbols_as_of(timestamptz) is
  'Tickers as they were known at a point in time. The source of truth for a historical symbol; ref.listings.local_code is only the current materialised value.';

-- The return type changes, so the function has to be dropped rather than replaced.
drop function if exists ref.listings_as_of(timestamptz);

create function ref.listings_as_of(p_available_at timestamptz)
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
   and st.available_at <= p_available_at
   and (st.effective_to is null or st.effective_to > p_available_at)
  left join lateral (
    select s.symbol, s.effective_from
    from ref.listing_symbols_as_of(p_available_at) s
    where s.listing_id = l.listing_id and s.symbol_type = 'TICKER'
    order by s.effective_from desc
    limit 1
  ) sym on true;
$$;

comment on function ref.listings_as_of(timestamptz) is
  'The listing master as it was known at a point in time: versioned attributes from ref.listing_states and the ticker that was current then from ref.listing_symbols. No column is read from the materialised current row.';

grant execute on function ref.listings_as_of(timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function ref.listing_symbols_as_of(timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;

comment on column ref.listings.local_code is
  'Current materialised provider code (for a US listing, the current ticker). Never the source of truth for a historical ticker: use ref.listing_symbols / ref.listing_symbols_as_of.';
