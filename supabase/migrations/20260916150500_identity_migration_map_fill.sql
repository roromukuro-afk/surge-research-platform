-- Phase 1.1: link the recorded Phase 1 identifiers to the rebuilt ones.
--
-- 20260916150400 recorded every Phase 1 security, listing and issuer id before
-- the master was cleared. Once the master has been reloaded under the new
-- identity rules, this fills in the new ids so that old -> new is auditable.
--
-- Both statements are idempotent (they only touch unmapped rows) and are a
-- no-op on a database that never held the Phase 1 master.

-- Listed records: the Phase 1 listing key was (exchange, local_code), which is
-- still the provider's coordinate for the same listing slot.
update ref.identity_migration_map m
set new_listing_id = l.listing_id,
    new_security_id = l.security_id,
    new_issuer_id = s.issuer_id
from ref.listings l
join ref.securities s on s.security_id = l.security_id
where m.migration_label = 'phase-1.1-identity-rebuild'
  and m.new_security_id is null
  and m.exchange_id is not distinct from l.exchange_id
  and m.local_code = l.local_code;

-- Records the provider gave no usable exchange for had no listing under either
-- scheme, so they are matched by recomputing the Phase 1 deterministic key
-- (security|<market>|NA|<symbol>) from the new provisional identity key.
update ref.identity_migration_map m
set new_security_id = s.security_id,
    new_issuer_id = s.issuer_id,
    symbol = coalesce(m.symbol, split_part(s.identity_key, ':', 4)),
    note = m.note || '; unlisted record (provider exchange not mapped): matched by the Phase 1 deterministic key'
from ref.securities s
where m.migration_label = 'phase-1.1-identity-rebuild'
  and m.new_security_id is null
  and s.identity_key like (m.market_code::text || ':NA:SYMBOL:%')
  and m.old_security_id = ref.deterministic_uuid(
        'security|' || m.market_code::text || '|NA|' || split_part(s.identity_key, ':', 4));
