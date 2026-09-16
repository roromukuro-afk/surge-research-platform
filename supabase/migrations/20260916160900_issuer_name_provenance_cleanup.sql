-- Phase 1.1a follow-up, part 2: finish what 20260916160800 started.
--
-- 1. Two functions were created *after* the revoke loop in 20260916160800 ran,
--    so they kept the Postgres default of EXECUTE for PUBLIC.
-- 2. ref.issuer_names rows written before 20260916160800 still carry the
--    security's source_record_id next to the registry's source_id. The column is
--    nulled: a provider record id identifies a security, never an issuer name.

revoke all on function ref.refresh_issuer_name_matching(uuid) from public;
revoke all on function ref.apply_master_snapshot_with_name_refresh(uuid) from public;

grant execute on function
  ref.refresh_issuer_name_matching(uuid),
  ref.apply_master_snapshot_with_name_refresh(uuid)
to surge_worker_prod;

update ref.issuer_names
set source_record_id = null
where source_record_id is not null;
