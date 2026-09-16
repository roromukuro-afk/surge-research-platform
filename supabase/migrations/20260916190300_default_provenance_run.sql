-- Phase 2.0 carry-forward D, completed: a new row already has provenance.
--
-- 20260916190000 backfilled the existing rows and made the refresh functions
-- maintain last_provenance_run_id, but ref.apply_master_snapshot inserts new
-- rows without it, so every newly created identifier or name sat at null until
-- a refresh happened to touch it. The run that created the row is exactly the
-- run that established its provenance, so say so at insert time.

create or replace function ref.default_provenance_run()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.last_provenance_run_id is null then
    new.last_provenance_run_id := new.ingestion_run_id;
  end if;
  return new;
end;
$$;

comment on function ref.default_provenance_run() is
  'A row version''s provenance starts out established by the run that created it. Later confirmations move last_provenance_run_id; ingestion_run_id stays.';

drop trigger if exists default_provenance_run on ref.security_identifiers;
create trigger default_provenance_run
  before insert on ref.security_identifiers
  for each row execute function ref.default_provenance_run();

drop trigger if exists default_provenance_run on ref.issuer_names;
create trigger default_provenance_run
  before insert on ref.issuer_names
  for each row execute function ref.default_provenance_run();

update ref.security_identifiers set last_provenance_run_id = ingestion_run_id where last_provenance_run_id is null;
update ref.issuer_names set last_provenance_run_id = ingestion_run_id where last_provenance_run_id is null;
