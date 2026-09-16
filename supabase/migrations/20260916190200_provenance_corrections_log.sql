-- Phase 2.0 carry-forward E: a correction is itself a fact, so record it.
--
-- Phase 1.1c said that moving available_at later "never retro-hides a row from
-- an as-of read that has already been answered". That is only true of answers
-- already given. A past-cutoff query asked AGAIN, after a correction, can return
-- a different answer - that is the point of a correction. What must never change
-- is a PUBLISHED RUN's artifacts, and those are frozen by the Phase 1.1c
-- triggers.
--
-- So the honest model is:
--   * published run artifacts            -> immutable
--   * ref master metadata (provenance)   -> correctable, and a correction may
--                                           change the answer to a future
--                                           past-cutoff query
--   * the correction itself              -> recorded here, append-only
--
-- Which of the two a Historical Replay reads is a choice the replay must state:
-- the published run reproduces what the system actually concluded at the time;
-- the corrected master reproduces what it should have concluded. See
-- docs/specs/security-identity.md section 4.

create table if not exists ref.provenance_corrections (
  correction_id bigint generated always as identity primary key,
  table_name text not null,
  row_id uuid not null,
  corrected_at timestamptz not null default clock_timestamp(),
  run_id uuid references pipeline.runs (run_id),
  changed_columns text[] not null,
  before_value jsonb not null,
  after_value jsonb not null
);

comment on table ref.provenance_corrections is
  'Append-only log of corrections to ref master provenance. A corrected row answers future past-cutoff queries differently from before; this is the record of what was corrected, when, and by which run.';
comment on column ref.provenance_corrections.run_id is
  'The run that made the correction (the row''s new last_provenance_run_id).';

create index if not exists provenance_corrections_row_idx
  on ref.provenance_corrections (table_name, row_id, corrected_at desc);
create index if not exists provenance_corrections_run_idx
  on ref.provenance_corrections (run_id);

-- ------------------------------------------------------------- the recorder
create or replace function ref.log_provenance_correction()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  -- last_confirmed_at and effective_to are re-observation and versioning, not
  -- correction: logging them would bury the corrections under 30k rows a day.
  v_tracked constant text[] := array[
    'source_id', 'source_record_id', 'observed_at', 'available_at', 'normalized_name'
  ];
  v_old jsonb := to_jsonb(old);
  v_new jsonb := to_jsonb(new);
  v_changed text[];
begin
  select coalesce(array_agg(k), '{}')
    into v_changed
  from unnest(v_tracked) as k
  where v_new ? k and (v_new -> k) is distinct from (v_old -> k);

  if coalesce(array_length(v_changed, 1), 0) = 0 then
    return null;
  end if;

  insert into ref.provenance_corrections (
    table_name, row_id, run_id, changed_columns, before_value, after_value
  )
  select tg_table_name,
         (v_new ->> tg_argv[0])::uuid,
         new.last_provenance_run_id,
         v_changed,
         jsonb_object_agg(k, v_old -> k),
         jsonb_object_agg(k, v_new -> k)
  from unnest(v_changed) as k;

  return null;
end;
$$;

comment on function ref.log_provenance_correction() is
  'Records a change to an existing row''s provenance in ref.provenance_corrections. Fires only when a tracked column actually changed.';

drop trigger if exists log_provenance_correction on ref.security_identifiers;
create trigger log_provenance_correction
  after update on ref.security_identifiers
  for each row execute function ref.log_provenance_correction('identifier_id');

drop trigger if exists log_provenance_correction on ref.issuer_names;
create trigger log_provenance_correction
  after update on ref.issuer_names
  for each row execute function ref.log_provenance_correction('issuer_name_id');

-- ------------------------------------------------------------- append-only
create or replace function ref.forbid_correction_rewrite()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'ref.provenance_corrections is append-only; a correction record cannot be % ', lower(tg_op)
    using errcode = 'read_only_sql_transaction';
end;
$$;

drop trigger if exists forbid_correction_rewrite on ref.provenance_corrections;
create trigger forbid_correction_rewrite
  before update or delete on ref.provenance_corrections
  for each row execute function ref.forbid_correction_rewrite();

drop trigger if exists forbid_correction_truncate on ref.provenance_corrections;
create trigger forbid_correction_truncate
  before truncate on ref.provenance_corrections
  for each statement execute function ref.forbid_correction_rewrite();

revoke all on table ref.provenance_corrections from public;
grant select, insert on table ref.provenance_corrections to surge_worker_prod;
grant select on table ref.provenance_corrections to surge_worker_research, surge_readonly;
