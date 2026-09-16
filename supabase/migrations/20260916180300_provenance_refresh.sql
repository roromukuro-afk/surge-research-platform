-- Phase 1.1c: correct the provenance of rows that already exist.
--
-- 20260916180100 fixed what the snapshot functions produce, but the SCD2 apply
-- only opens a new row when the VALUE changes. An identifier whose value was
-- unchanged kept its old, wrong provenance: the CIK still said Nasdaq had
-- supplied it. Provenance is not the value, so correcting it does not open a new
-- version - it repairs the record of where the existing value came from.
--
-- available_at only ever moves LATER (greatest): a row that was visible too
-- early is corrected, while one that was already visible is never retro-hidden
-- from an as-of read that has already been answered.

create or replace function ref.refresh_identifier_provenance(p_run_id uuid)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  update ref.security_identifiers si
  set source_id = i.source_id,
      source_record_id = i.source_record_id,
      observed_at = i.observed_at,
      available_at = greatest(si.available_at, i.available_at)
  from pipeline.snapshot_identifiers(p_run_id) i
  where si.security_id = i.security_id
    and si.id_type = i.id_type
    and si.id_namespace is not distinct from i.id_namespace
    and si.effective_to is null
    and si.id_value = i.id_value
    and (si.source_id is distinct from i.source_id
      or si.source_record_id is distinct from i.source_record_id
      or si.observed_at is distinct from i.observed_at
      or si.available_at < i.available_at);
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function ref.refresh_identifier_provenance(uuid) is
  'Repairs where an existing identifier says it came from, without opening a new version. available_at only moves later.';

create or replace function ref.refresh_issuer_name_matching(p_run_id uuid)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_rows int;
begin
  update ref.issuer_names ins
  set normalized_name = i.normalized_name,
      source_id = i.name_source,
      observed_at = i.observed_at,
      available_at = greatest(ins.available_at, i.available_at)
  from pipeline.snapshot_issuers(p_run_id) i
  where ins.issuer_id = i.issuer_id
    and ins.name_type = i.name_type
    and ins.effective_to is null
    and (ins.normalized_name is distinct from i.normalized_name
      or ins.source_id is distinct from i.name_source
      or ins.observed_at is distinct from i.observed_at
      or ins.available_at < i.available_at);
  get diagnostics v_rows = row_count;
  return v_rows;
end;
$$;

comment on function ref.refresh_issuer_name_matching(uuid) is
  'Brings an existing issuer name row''s matching key and provenance in line with the snapshot. The name itself is never changed here: that is a new version.';

create or replace function ref.apply_master_snapshot_with_name_refresh(p_run_id uuid)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_result jsonb;
  v_names int;
  v_identifiers int;
begin
  v_result := ref.apply_master_snapshot(p_run_id);
  v_names := ref.refresh_issuer_name_matching(p_run_id);
  v_identifiers := ref.refresh_identifier_provenance(p_run_id);
  return v_result || jsonb_build_object(
    'issuer_names_rematched', v_names,
    'identifier_provenance_repaired', v_identifiers
  );
end;
$$;

comment on function ref.apply_master_snapshot_with_name_refresh(uuid) is
  'ref.apply_master_snapshot plus the provenance repairs. This is what the runbook and the rebuild script call.';

revoke all on function
  ref.refresh_identifier_provenance(uuid),
  ref.refresh_issuer_name_matching(uuid),
  ref.apply_master_snapshot_with_name_refresh(uuid)
from public;

grant execute on function
  ref.refresh_identifier_provenance(uuid),
  ref.refresh_issuer_name_matching(uuid),
  ref.apply_master_snapshot_with_name_refresh(uuid)
to surge_worker_prod;
