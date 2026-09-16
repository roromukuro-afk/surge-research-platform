-- Phase 1.1: rebuild the security master under the new identity rules.
--
-- Phase 1 rows were keyed on (exchange, local_code), which is the ticker for US
-- listings, and on normalised names for issuers. Those identifiers cannot be
-- migrated in place, so the master is rebuilt from freshly fetched provider
-- snapshots. What was there before is recorded first, so the rebuild is
-- auditable instead of silent.
--
-- On an empty database (CI, a fresh environment) this migration does nothing.

do $$
declare
  v_old_securities int;
begin
  select count(*) into v_old_securities from ref.securities;
  if v_old_securities = 0 then
    raise notice 'no existing master: nothing to record or clear';
    return;
  end if;

  insert into ref.identity_migration_map (
    migration_label, market_code, exchange_id, local_code, symbol,
    old_security_id, old_listing_id, old_issuer_id, note
  )
  select 'phase-1.1-identity-rebuild',
         s.market_code,
         l.exchange_id,
         coalesce(l.local_code, 'UNKNOWN'),
         (select ls.symbol
          from ref.listing_symbols ls
          where ls.listing_id = l.listing_id and ls.effective_to is null
          limit 1),
         s.security_id,
         l.listing_id,
         s.issuer_id,
         'identity was derived from exchange + local_code (ticker) and normalised issuer name'
  from ref.securities s
  left join ref.listings l on l.security_id = s.security_id;

  -- Clear in dependency order. pipeline.runs and pipeline.source_fetches are kept:
  -- they are the record of what was ingested and when.
  delete from universe.evaluations;
  delete from universe.coverage;
  delete from ref.listing_symbols;
  delete from ref.listing_status_history;
  delete from ref.listing_states;
  delete from ref.security_identifiers;
  delete from ref.security_names;
  delete from ref.listings;
  delete from ref.securities;
  delete from ref.issuers;
  delete from pipeline.master_snapshot;

  raise notice 'phase 1.1 rebuild: recorded % securities and cleared the master for reload', v_old_securities;
end
$$;
