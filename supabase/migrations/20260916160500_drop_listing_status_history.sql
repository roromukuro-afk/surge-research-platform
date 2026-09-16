-- Phase 1.1a: remove the superseded listing status history table.
--
-- ref.listing_status_history was written by the Phase 1 apply function. Since
-- 20260916150100 the state history lives in ref.listing_states (segment, name
-- and status versioned together, with last_confirmed_at and an open-row
-- constraint), and the old table has been empty ever since.
--
-- Leaving an empty, plausible looking history table in place invites a future
-- query to read it and conclude that nothing ever changed. It is dropped rather
-- than deprecated: ref.listing_states is the only source of truth for listing
-- state over time.

do $$
declare
  v_rows bigint;
begin
  if not exists (
    select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'ref' and c.relname = 'listing_status_history'
  ) then
    raise notice 'ref.listing_status_history is already gone';
    return;
  end if;

  execute 'select count(*) from ref.listing_status_history' into v_rows;
  if v_rows > 0 then
    -- Never destroy history: if some environment did populate it, keep the
    -- table and mark it instead of dropping data.
    execute 'comment on table ref.listing_status_history is ''DEPRECATED (Phase 1.1a): superseded by ref.listing_states. Not written by any current function. Kept because it holds rows.''';
    raise notice 'ref.listing_status_history holds % rows: marked DEPRECATED instead of dropped', v_rows;
    return;
  end if;

  execute 'drop table ref.listing_status_history';
  raise notice 'ref.listing_status_history dropped (was empty; superseded by ref.listing_states)';
end
$$;
