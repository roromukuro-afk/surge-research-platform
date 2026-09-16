-- Phase 1.1a: make the rebuild procedure reproducible instead of implicit.
--
-- The Phase 1.1 rebuild was two migrations with a manual step between them:
-- 20260916150400 recorded the old ids and cleared the master, and 20260916150500
-- filled in the new ids - but only after the operator had re-fetched the
-- providers and reloaded the snapshot. Running the migrations back to back (a
-- plain `db push` against an old database) executes the fill against an empty
-- master and silently maps nothing.
--
-- The fill is therefore a procedure that an operator runs *after* the reload,
-- and it refuses to run before it. The runbook and scripts/rebuild_security_master.sh
-- carry the whole sequence.

create or replace function ref.finalize_identity_rebuild(p_label text)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_recorded int;
  v_by_listing int;
  v_by_identity int;
  v_by_legacy_key int;
  v_unresolved int;
  v_changed_security int;
  v_changed_issuer int;
  v_changed_listing int;
begin
  select count(*) into v_recorded
  from ref.identity_migration_map where migration_label = p_label;

  if v_recorded = 0 then
    raise exception 'no rows recorded for migration label %: nothing to finalize', p_label;
  end if;

  if (select count(*) from ref.securities) = 0 then
    raise exception
      'the master is empty: re-run the provider sync and load the snapshot before finalizing %', p_label;
  end if;

  -- 1. listed records: the provider coordinate (exchange, local code) still
  --    identifies the same listing slot.
  update ref.identity_migration_map m
  set new_listing_id = l.listing_id,
      new_security_id = l.security_id,
      new_issuer_id = s.issuer_id
  from ref.listings l
  join ref.securities s on s.security_id = l.security_id
  where m.migration_label = p_label
    and m.new_security_id is null
    and m.exchange_id is not distinct from l.exchange_id
    and m.local_code = l.local_code;
  get diagnostics v_by_listing = row_count;

  -- 2. identifiers that did not change at all (a rebuild that only re-keyed
  --    issuers, for example) map to themselves.
  update ref.identity_migration_map m
  set new_security_id = s.security_id,
      new_issuer_id = s.issuer_id
  from ref.securities s
  where m.migration_label = p_label
    and m.new_security_id is null
    and m.old_security_id = s.security_id;
  get diagnostics v_by_identity = row_count;

  -- 3. records the provider gave no usable exchange for had no listing under
  --    either scheme: recompute the Phase 1 deterministic key from the new
  --    provisional identity key.
  update ref.identity_migration_map m
  set new_security_id = s.security_id,
      new_issuer_id = s.issuer_id,
      symbol = coalesce(m.symbol, split_part(s.identity_key, ':', 4)),
      note = coalesce(m.note, '') || '; unlisted record: matched by the Phase 1 deterministic key'
  from ref.securities s
  where m.migration_label = p_label
    and m.new_security_id is null
    and s.identity_key like (m.market_code::text || ':NA:SYMBOL:%')
    and m.old_security_id = ref.deterministic_uuid(
          'security|' || m.market_code::text || '|NA|' || split_part(s.identity_key, ':', 4));
  get diagnostics v_by_legacy_key = row_count;

  select count(*) filter (where new_security_id is null),
         count(*) filter (where new_security_id is not null and new_security_id <> old_security_id),
         count(*) filter (where new_issuer_id is not null and new_issuer_id <> old_issuer_id),
         count(*) filter (where new_listing_id is not null and new_listing_id <> old_listing_id)
    into v_unresolved, v_changed_security, v_changed_issuer, v_changed_listing
  from ref.identity_migration_map
  where migration_label = p_label;

  return jsonb_build_object(
    'migration_label', p_label,
    'recorded', v_recorded,
    'matched_by_listing', v_by_listing,
    'matched_by_unchanged_id', v_by_identity,
    'matched_by_legacy_key', v_by_legacy_key,
    'unresolved', v_unresolved,
    'security_ids_changed', v_changed_security,
    'issuer_ids_changed', v_changed_issuer,
    'listing_ids_changed', v_changed_listing
  );
end;
$$;

comment on function ref.finalize_identity_rebuild(text) is
  'Fills old -> new ids after a rebuild has been reloaded from the official sources. Refuses to run against an empty master, so applying the migrations alone can never look like a completed migration.';

-- Administrator procedure, not a runtime one: the worker has SELECT only on
-- ref.identity_migration_map since 20260916160000.
revoke all on function ref.finalize_identity_rebuild(text) from public;
