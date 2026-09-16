-- Phase 1.1a: rebuild the master so the corrected identities and names apply.
--
-- Three of the Phase 1.1a changes cannot be patched in place:
--   * issuers without a registry identifier are now keyed on their security's
--     coordinate rather than on a normalised name, so those issuer_ids change;
--   * issuer legal names come from the registry instead of from one of the
--     issuer's products;
--   * US security identities are REGISTRY_ANCHORED rather than STRONG.
--
-- Security and listing ids are unaffected (their keys did not change), which the
-- old -> new map will show. As in Phase 1.1: record first, clear, reload from
-- the official sources, then run ref.finalize_identity_rebuild.
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
  select 'phase-1.1a-identity-rebuild',
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
         'issuer identity was name-derived for provisional issuers; issuer legal name was a security name'
  from ref.securities s
  left join ref.listings l on l.security_id = s.security_id;

  delete from universe.evaluations;
  delete from universe.coverage;
  delete from ref.listing_symbols;
  delete from ref.listing_states;
  delete from ref.security_identifiers;
  delete from ref.security_names;
  delete from ref.issuer_names;
  delete from ref.listings;
  delete from ref.securities;
  delete from ref.issuers;
  delete from pipeline.master_snapshot;

  raise notice 'phase 1.1a rebuild: recorded % securities and cleared the master for reload', v_old_securities;
end
$$;
