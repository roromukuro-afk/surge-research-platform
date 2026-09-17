-- Phase 8 hardening: make the guards hold against the database, not just the job.
--
-- The Phase 8 guards were written assuming callers arrive through
-- ``surge.entry``. Four of them do not survive a direct write, and one of those
-- was reachable on the live database:
--
--   A. ``case new.from_state when null then ...`` never matches. A simple CASE
--      compares with ``=``, and ``NULL = NULL`` is NULL, so a transition with a
--      NULL ``from_state`` fell through to ``legal := NULL``; ``if not NULL``
--      is not true, so nothing was raised. Probed against the cloud: a brand
--      new watch went straight to ENTERED. The trigger also trusted
--      ``from_state`` as declared rather than reading the watch's actual state,
--      so a caller could simply claim to be somewhere else.
--   B. A prediction had to match three of its attempt's fields. Everything else
--      - the security, the thesis, the yen values, the provider, the cutoff -
--      could differ, which is exactly the shape of an honest attempt with a
--      flattering prediction written beside it.
--   C. ``prod.setups`` was mutable and ``prod.episodes`` was mutable in every
--      column, so a closed episode could be reopened and an entry time moved.
--   D. The +20% target was checked with a tolerance. Two numerics do not need
--      one, and a tolerance is a place where a wrong number can hide.
--
-- The theme: a guard that assumes its caller is not a guard.

-- ---------------------------------------------------------------------------
-- A. The watch machine, read from the watch rather than from the caller
-- ---------------------------------------------------------------------------

--: The head row moves only from inside the transition trigger. The flag is
--: transaction-local and set immediately before the update, so an ordinary
--: UPDATE - by the worker, by a script, by hand - has nothing to hide behind.
create or replace function prod.guard_watch_head()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'prod.watches rows are not deleted; a finished watch is EXPIRED or REJECTED';
  end if;
  if coalesce(current_setting('prod.moving_watch', true), 'off') <> 'on' then
    raise exception
      'prod.watches.state is moved by inserting into prod.watch_transitions, not by updating this row (watch %)',
      old.watch_id;
  end if;
  if new.watch_id is distinct from old.watch_id
     or new.setup_id is distinct from old.setup_id
     or new.security_id is distinct from old.security_id
     or new.trigger_description is distinct from old.trigger_description
     or new.opened_at is distinct from old.opened_at
     or new.verification is distinct from old.verification then
    raise exception 'only state and updated_at may change on prod.watches (watch %)', old.watch_id;
  end if;
  return new;
end;
$$;

drop trigger if exists prod_watches_head_guard on prod.watches;
create trigger prod_watches_head_guard
  before update or delete on prod.watches
  for each row execute function prod.guard_watch_head();

--: A watch starts ARMED and belongs to its setup's security. Both were
--: conventions; a watch created in some other state would start the machine
--: half way through it.
create or replace function prod.check_watch_creation()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  setup_security uuid;
begin
  if new.state <> 'ARMED' then
    raise exception
      'a watch starts ARMED, not %; later states are reached by inserting transitions', new.state;
  end if;
  select s.security_id into setup_security from prod.setups s where s.setup_id = new.setup_id;
  if setup_security is null then
    raise exception 'watch references a missing setup %', new.setup_id;
  end if;
  if setup_security <> new.security_id then
    raise exception
      'watch is on security % but its setup % is on %; a watch cannot belong to a different security than the setup that produced it',
      new.security_id, new.setup_id, setup_security;
  end if;
  return new;
end;
$$;

drop trigger if exists prod_watches_creation on prod.watches;
create trigger prod_watches_creation
  before insert on prod.watches
  for each row execute function prod.check_watch_creation();

--: The real machine. Reads the current state under a row lock, refuses a
--: declared from_state that disagrees with it, and handles the initial
--: transition with an explicit IS NULL rather than a CASE branch that cannot
--: match.
create or replace function prod.check_watch_transition()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  current_state prod.watch_state;
  legal boolean;
begin
  select w.state into current_state
    from prod.watches w
   where w.watch_id = new.watch_id
     for update;

  if not found then
    raise exception 'watch % does not exist', new.watch_id;
  end if;

  if new.from_state is null then
    -- The only transition that may declare no origin is the first one, and a
    -- watch is only at the start once.
    if new.to_state <> 'ARMED' then
      raise exception
        'a transition with no from_state is the watch being armed; % is not ARMED (watch %)',
        new.to_state, new.watch_id;
    end if;
    if exists (select 1 from prod.watch_transitions t where t.watch_id = new.watch_id) then
      raise exception
        'watch % is already armed; a second opening transition would restart the machine',
        new.watch_id;
    end if;
    return new;
  end if;

  -- The caller says where it thinks the watch is. The watch says where it is.
  -- They have to agree, or the machine is being driven from a stale view.
  if new.from_state is distinct from current_state then
    raise exception
      'watch % is in state %, not % as declared; the transition was computed against a stale read',
      new.watch_id, current_state, new.from_state;
  end if;

  -- The edge that must not exist. CLAUDE.md 1-5: reaching a watch condition is
  -- an observation; entering is a decision, and a decision needs an analysis
  -- between it and the observation.
  if current_state = 'TRIGGER_HIT' and new.to_state = 'ENTERED' then
    raise exception
      'a watch cannot go from TRIGGER_HIT straight to ENTERED; a REANALYSIS must run first (watch %)',
      new.watch_id;
  end if;

  if new.to_state = 'ENTERED' then
    if current_state <> 'IN_REANALYSIS' then
      raise exception
        'ENTERED is only reachable from IN_REANALYSIS, not from % (watch %)',
        current_state, new.watch_id;
    end if;
    if new.analysis_kind is distinct from 'REANALYSIS' then
      raise exception
        'entering from a watch requires analysis_kind = REANALYSIS, got % (watch %)',
        coalesce(new.analysis_kind::text, 'null'), new.watch_id;
    end if;
  end if;

  legal := case current_state
    when 'ARMED' then new.to_state in ('TRIGGER_HIT', 'EXPIRED')
    when 'TRIGGER_HIT' then new.to_state in ('IN_REANALYSIS', 'EXPIRED')
    when 'IN_REANALYSIS' then new.to_state in ('ENTERED', 'REJECTED', 'REARMED', 'EXPIRED')
    when 'REARMED' then new.to_state in ('TRIGGER_HIT', 'EXPIRED')
    when 'REJECTED' then false
    when 'ENTERED' then false
    when 'EXPIRED' then false
    else false
  end;

  if legal is not true then
    raise exception 'illegal watch transition % -> % (watch %)',
      current_state, new.to_state, new.watch_id;
  end if;

  perform set_config('prod.moving_watch', 'on', true);
  update prod.watches
     set state = new.to_state, updated_at = clock_timestamp()
   where watch_id = new.watch_id;
  perform set_config('prod.moving_watch', 'off', true);

  return new;
end;
$$;

comment on function prod.check_watch_transition() is
  'The watch machine. Reads the current state under a row lock rather than trusting the declared from_state, handles the initial NULL origin with an explicit IS NULL (a simple CASE cannot match NULL, which is how a fresh watch reached ENTERED directly), and moves the head row itself so that prod.watches never has to be updatable by a caller.';

--: The head row is no longer the worker's to write.
revoke update on prod.watches from surge_worker_prod;
grant select, insert on prod.watches to surge_worker_prod;

-- ---------------------------------------------------------------------------
-- B. A prediction is its attempt, or it is not a prediction
-- ---------------------------------------------------------------------------

create or replace function prod.check_prediction_matches_attempt()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  a prod.entry_attempts%rowtype;
  e prod.episodes%rowtype;
  mismatched text[] := '{}';
begin
  select * into a from prod.entry_attempts where attempt_id = new.attempt_id;
  if not found then
    raise exception 'prediction % references a missing entry attempt', new.prediction_id;
  end if;
  if a.status <> 'PREDICTION_CREATED' then
    raise exception
      'prediction % comes from an attempt whose status is % - only PREDICTION_CREATED may produce one',
      new.prediction_id, a.status;
  end if;

  -- IS DISTINCT FROM throughout: a NULL on one side and a value on the other is
  -- a mismatch, and two NULLs are not.
  if a.security_id is distinct from new.security_id then
    mismatched := array_append(mismatched, 'security_id');
  end if;
  if a.thesis_key is distinct from new.thesis_key then
    mismatched := array_append(mismatched, 'thesis_key');
  end if;
  if a.analysis_kind is distinct from new.analysis_kind then
    mismatched := array_append(mismatched, 'analysis_kind');
  end if;
  if a.decision_price is distinct from new.decision_price then
    mismatched := array_append(mismatched, 'decision_price');
  end if;
  if a.decision_price_observed_at is distinct from new.decision_price_observed_at then
    mismatched := array_append(mismatched, 'decision_price_observed_at');
  end if;
  if a.decision_price_jpy is distinct from new.decision_price_jpy then
    mismatched := array_append(mismatched, 'decision_price_jpy');
  end if;
  if a.entry_reference_price is distinct from new.entry_reference_price then
    mismatched := array_append(mismatched, 'entry_reference_price');
  end if;
  if a.entry_price_observed_at is distinct from new.entry_price_observed_at then
    mismatched := array_append(mismatched, 'entry_price_observed_at');
  end if;
  if a.entry_price_jpy is distinct from new.entry_price_jpy then
    mismatched := array_append(mismatched, 'entry_price_jpy');
  end if;
  if a.entry_price_method is distinct from new.entry_price_method then
    mismatched := array_append(mismatched, 'entry_price_method');
  end if;
  if a.universe_decision is distinct from new.universe_decision then
    mismatched := array_append(mismatched, 'universe_decision');
  end if;
  -- The prediction's data cutoff is the attempt's decision cutoff. A later one
  -- would mean the claim was formed from data the decision did not have.
  if a.decision_cutoff_at is distinct from new.data_cutoff then
    mismatched := array_append(mismatched, 'data_cutoff/decision_cutoff_at');
  end if;
  if a.provider_id is distinct from new.provider_id then
    mismatched := array_append(mismatched, 'provider_id');
  end if;
  if a.verification is distinct from new.verification then
    mismatched := array_append(mismatched, 'verification');
  end if;
  if a.run_id is distinct from new.run_id then
    mismatched := array_append(mismatched, 'run_id');
  end if;

  if array_length(mismatched, 1) is not null then
    raise exception
      'prediction % disagrees with its attempt % on: %',
      new.prediction_id, new.attempt_id, array_to_string(mismatched, ', ');
  end if;

  -- And the episode has to be about the same claim.
  select * into e from prod.episodes where episode_id = new.episode_id;
  if not found then
    raise exception 'prediction % references a missing episode', new.prediction_id;
  end if;
  if e.security_id is distinct from new.security_id then
    raise exception
      'prediction % is on security % but episode % is on %',
      new.prediction_id, new.security_id, e.episode_id, e.security_id;
  end if;
  if e.thesis_key is distinct from new.thesis_key then
    raise exception
      'prediction % is under thesis % but episode % is under %',
      new.prediction_id, new.thesis_key, e.episode_id, e.thesis_key;
  end if;
  if e.entry_price_observed_at is distinct from new.entry_price_observed_at then
    raise exception
      'prediction % entered at % but episode % starts its horizon at %; S0 and the entry are the same moment',
      new.prediction_id, new.entry_price_observed_at, e.episode_id, e.entry_price_observed_at;
  end if;

  return new;
end;
$$;

comment on function prod.check_prediction_matches_attempt() is
  'A prediction must carry its attempt''s own numbers and belong to an episode about the same security, thesis and entry moment. The failure this exists for is an honest attempt with a flattering prediction written beside it.';

-- ---------------------------------------------------------------------------
-- C. Mutation boundaries
-- ---------------------------------------------------------------------------

--: Setups join the other five append-only tables. A setup that can be edited
--: after the fact is a record of what we wish we had said.
drop trigger if exists prod_setups_append_only on prod.setups;
create trigger prod_setups_append_only
  before update or delete on prod.setups
  for each row execute function prod.forbid_mutation();

--: Episodes have exactly one legal mutation: OPEN -> CLOSED, once.
create or replace function prod.guard_episode_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'prod.episodes is not deleted from; an episode that ended is CLOSED with a reason';
  end if;

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
  'CLOSED is terminal and the identity columns are frozen. Reopening a closed episode, or moving the entry moment it counts sessions from, would rewrite an outcome after seeing it.';

drop trigger if exists prod_episodes_guard on prod.episodes;
create trigger prod_episodes_guard
  before update or delete on prod.episodes
  for each row execute function prod.guard_episode_update();

-- ---------------------------------------------------------------------------
-- D. The +20% target, without a tolerance
-- ---------------------------------------------------------------------------

alter table prod.predictions drop constraint if exists predictions_target_is_twenty_percent;
alter table prod.predictions add constraint predictions_target_is_twenty_percent
  check (target_price = round(entry_reference_price * 1.20, 6));

comment on constraint predictions_target_is_twenty_percent on prod.predictions is
  'Exact, not within a tolerance. Both sides are numeric, so there is nothing to tolerate - and a tolerance is a place where a wrong number can sit undetected. round() here matches Decimal ROUND_HALF_UP in surge.entry.models.target_for.';

-- ---------------------------------------------------------------------------
-- Grants for the new functions
-- ---------------------------------------------------------------------------

revoke all on function prod.guard_watch_head() from public;
revoke all on function prod.check_watch_creation() from public;
revoke all on function prod.guard_episode_update() from public;
revoke all on function prod.check_watch_transition() from public;
revoke all on function prod.check_prediction_matches_attempt() from public;
