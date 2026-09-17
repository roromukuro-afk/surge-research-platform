-- Phase 9 hardening: an episode closes through one door, and only when decided.
--
-- Two gaps, both of the same kind - a rule the application follows that the
-- database does not require:
--
--   * ``surge_worker_prod`` could UPDATE ``prod.episodes`` directly, so it could
--     take an episode OPEN -> CLOSED and simply never write the outcome. The
--     "atomic" close was atomic only for callers who used the function.
--   * Nothing stopped a writer recording HORIZON_EXPIRED at S8. The application
--     now refuses to, but HORIZON_EXPIRED means "neither line, by S20" and a
--     future writer should not be able to say it at S8 either.
--
-- After this migration the runtime has no write privilege on either table.
-- ``prod.close_episode_with_outcome()`` is the only door, it holds a
-- transaction-local flag while it works, and the guards refuse anything that
-- arrives without it.

-- ---------------------------------------------------------------------------
-- The episode guard, with the flag
-- ---------------------------------------------------------------------------

create or replace function prod.guard_episode_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'prod.episodes is not deleted from; an episode that ended is CLOSED with a reason';
  end if;

  -- Checked first so that an attempt to edit an identity column gets the
  -- specific answer rather than the generic one, whichever door it came through.
  if new.episode_id is distinct from old.episode_id
     or new.security_id is distinct from old.security_id
     or new.thesis_key is distinct from old.thesis_key
     or new.opened_at is distinct from old.opened_at
     or new.entry_price_observed_at is distinct from old.entry_price_observed_at
     or new.horizon_sessions is distinct from old.horizon_sessions
     or new.verification is distinct from old.verification
     or new.created_at is distinct from old.created_at then
    raise exception
      'episode %: only status, closed_at and close_reason may change. Moving entry_price_observed_at would move S0 and therefore the horizon',
      old.episode_id;
  end if;

  -- Closing is not a write anyone may make on its own. It happens inside
  -- prod.close_episode_with_outcome(), which records how the episode ended in
  -- the same call - so a closed episode with no outcome cannot be produced.
  if coalesce(current_setting('prod.closing_episode', true), 'off') <> 'on' then
    raise exception
      'episode % is closed by prod.close_episode_with_outcome(), not by updating this row. Closing and recording how it ended are one event',
      old.episode_id;
  end if;

  if old.status = 'CLOSED' then
    raise exception
      'episode % is closed (%); a closed episode is not reopened or re-decided',
      old.episode_id, old.close_reason;
  end if;

  if new.status is distinct from 'CLOSED' then
    raise exception
      'episode %: the only permitted update is OPEN -> CLOSED, not OPEN -> %',
      old.episode_id, new.status;
  end if;

  if new.closed_at is null or new.close_reason is null then
    raise exception 'episode %: closing needs both closed_at and close_reason', old.episode_id;
  end if;

  return new;
end;
$$;

comment on function prod.guard_episode_update() is
  'CLOSED is terminal, the identity columns are frozen, and the transition happens only inside prod.close_episode_with_outcome(). Without the flag a caller could close an episode and never record how it ended - which is the same failure as recording it wrongly, only quieter.';

-- ---------------------------------------------------------------------------
-- The outcome table: written by the close function, never updated
-- ---------------------------------------------------------------------------

create or replace function prod.check_outcome_matches_episode()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  e prod.episodes%rowtype;
begin
  if coalesce(current_setting('prod.closing_episode', true), 'off') <> 'on' then
    raise exception
      'prod.episode_outcomes is written by prod.close_episode_with_outcome(), not inserted directly. An outcome and the close it describes are one event';
  end if;

  select * into e from prod.episodes where episode_id = new.episode_id;
  if not found then
    raise exception 'outcome references a missing episode %', new.episode_id;
  end if;
  if e.status <> 'CLOSED' then
    raise exception
      'episode % is still open; an outcome is the record of how it ended', new.episode_id;
  end if;
  if new.primary_episode_outcome is distinct from e.close_reason then
    raise exception
      'outcome for episode % says % but the episode was closed for %; the two are the same fact and must not be written twice with different answers',
      new.episode_id, coalesce(new.primary_episode_outcome::text, 'null'), e.close_reason;
  end if;
  return new;
end;
$$;

drop trigger if exists prod_episode_outcomes_match on prod.episode_outcomes;
create trigger prod_episode_outcomes_match
  before insert on prod.episode_outcomes
  for each row execute function prod.check_outcome_matches_episode();

--: Append-only. An outcome that can be edited is a result that can be improved
--: after it is known, which is the one thing a result must not be.
drop trigger if exists prod_episode_outcomes_append_only on prod.episode_outcomes;
create trigger prod_episode_outcomes_append_only
  before update or delete on prod.episode_outcomes
  for each row execute function prod.forbid_mutation();

-- ---------------------------------------------------------------------------
-- HORIZON_EXPIRED means "neither line, by S20", at the database level
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'outcomes_expiry_needs_the_whole_horizon') then
    alter table prod.episode_outcomes add constraint outcomes_expiry_needs_the_whole_horizon
      check (
        primary_episode_outcome is distinct from 'HORIZON_EXPIRED'
        or (counterfactual_sessions_observed = 21
            and primary_resolved_session_index = 20)
      );
  end if;
  --: If the whole window was observed and neither line was reached, then the
  --: target demonstrably was not reached. Unknown is not available here.
  if not exists (select 1 from pg_constraint where conname = 'outcomes_expiry_means_target_not_reached') then
    alter table prod.episode_outcomes add constraint outcomes_expiry_means_target_not_reached
      check (
        primary_episode_outcome is distinct from 'HORIZON_EXPIRED'
        or counterfactual_later_target_hit is false
      );
  end if;
end $$;

comment on constraint outcomes_expiry_needs_the_whole_horizon on prod.episode_outcomes is
  'HORIZON_EXPIRED is a statement about S20, so it requires all 21 sessions (S0..S20) to have been observed and the resolution to sit at S20. Without this a future writer could record an expired horizon at S8 - a finished-looking record of an unfinished episode, on a table where closing happens once.';

-- ---------------------------------------------------------------------------
-- Unknown is not false
-- ---------------------------------------------------------------------------

comment on column prod.episode_outcomes.counterfactual_later_target_hit is
  'Three-valued. TRUE: the price was seen to reach the target. FALSE: the whole window was observed and it did not. NULL: not known - the window is incomplete, or the path was unresolvable. Defaulting NULL to FALSE would turn "we have not finished looking" into "it did not happen", which is the more flattering of the two.';

create or replace function prod.close_episode_with_outcome(
  p_episode_id uuid,
  p_primary_outcome prod.episode_close_reason,
  p_closed_at timestamptz,
  p_path_resolution prod.path_resolution default null,
  p_granularity text default null,
  p_resolved_session_index integer default null,
  p_resolved_at timestamptz default null,
  p_primary_detail text default null,
  p_counterfactual_path prod.path_resolution default null,
  p_later_target_hit boolean default null,
  p_later_target_hit_at timestamptz default null,
  p_later_target_hit_session_index integer default null,
  p_mfe numeric default null,
  p_mae numeric default null,
  p_sessions_observed integer default null,
  p_corporate_action_ids text[] default '{}',
  p_outcome_currency text default null,
  p_engine_version text default null,
  p_label_version text default null,
  p_notes text default null
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- Held for the rest of the transaction. If the insert below fails, the update
  -- above it rolls back with it and the episode is still OPEN - which is the
  -- property this function exists for.
  perform set_config('prod.closing_episode', 'on', true);

  update prod.episodes
     set status = 'CLOSED', closed_at = p_closed_at, close_reason = p_primary_outcome
   where episode_id = p_episode_id and status = 'OPEN';

  if not found then
    raise exception
      'episode % is not open; an episode is closed once and its outcome decided once', p_episode_id;
  end if;

  insert into prod.episode_outcomes (
    episode_id, primary_episode_outcome, primary_path_resolution,
    primary_resolution_granularity, primary_resolved_session_index, primary_resolved_at,
    primary_detail,
    counterfactual_path_resolution, counterfactual_later_target_hit,
    counterfactual_later_target_hit_at, counterfactual_later_target_hit_session_index,
    counterfactual_mfe, counterfactual_mae, counterfactual_sessions_observed,
    corporate_action_ids_applied, outcome_currency, engine_version, label_version, notes
  ) values (
    p_episode_id, p_primary_outcome, p_path_resolution,
    p_granularity, p_resolved_session_index, p_resolved_at,
    p_primary_detail,
    -- No coalesce. NULL means not known, and it stays NULL.
    p_counterfactual_path, p_later_target_hit,
    p_later_target_hit_at, p_later_target_hit_session_index,
    p_mfe, p_mae, p_sessions_observed,
    coalesce(p_corporate_action_ids, '{}'), p_outcome_currency, p_engine_version,
    p_label_version, p_notes
  );

  perform set_config('prod.closing_episode', 'off', true);
  return p_episode_id;
end;
$$;

comment on function prod.close_episode_with_outcome is
  'The only way an episode closes. Holds a transaction-local flag so the guards on both tables accept the pair and nothing else, which makes "closed with no outcome" unrepresentable rather than merely discouraged. A failure in the insert rolls the close back with it.';

-- ---------------------------------------------------------------------------
-- The runtime no longer writes either table directly
-- ---------------------------------------------------------------------------

revoke update on prod.episodes from surge_worker_prod;
revoke insert, update on prod.episode_outcomes from surge_worker_prod;
grant select, insert on prod.episodes to surge_worker_prod;
grant select on prod.episode_outcomes to surge_worker_prod;
grant execute on function prod.close_episode_with_outcome to surge_worker_prod;
