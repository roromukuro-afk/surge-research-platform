-- Phase 9: outcomes, stored atomically with the close they describe.
--
-- An episode that is closed without an outcome, or an outcome whose verdict
-- disagrees with the reason the episode was closed for, are both records that
-- look complete and are not. Neither is possible after this migration: the two
-- writes happen inside one SECURITY DEFINER function, and a trigger refuses an
-- outcome whose primary verdict is not the episode's own close reason.
--
-- The two layers stay in separate columns. A +20% move after a thesis was
-- invalidated is ``counterfactual_later_target_hit`` and is never the primary
-- outcome; the database has no way to express it as one.

-- ---------------------------------------------------------------------------
-- What the engine produces that the table could not yet hold
-- ---------------------------------------------------------------------------

alter table prod.episode_outcomes
  add column if not exists primary_detail text,
  add column if not exists counterfactual_later_target_hit_session_index integer,
  add column if not exists counterfactual_sessions_observed integer,
  add column if not exists outcome_currency text,
  add column if not exists engine_version text,
  add column if not exists notes text;

comment on column prod.episode_outcomes.outcome_currency is
  'The security''s own currency. The outcome engine has no FX input at all, which is what stops a +17% move in USD becoming a +20% success because the yen moved (CLAUDE.md 1-9).';
comment on column prod.episode_outcomes.counterfactual_sessions_observed is
  'How many sessions were actually available, out of the 21 the horizon needs. Fewer than 21 with NEITHER_BY_HORIZON is a provisional read, not an expired horizon.';

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'outcomes_currency_shape'
  ) then
    alter table prod.episode_outcomes add constraint outcomes_currency_shape
      check (outcome_currency is null or outcome_currency ~ '^[A-Z]{3}$');
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'outcomes_sessions_within_horizon'
  ) then
    alter table prod.episode_outcomes add constraint outcomes_sessions_within_horizon
      check (
        counterfactual_sessions_observed is null
        or counterfactual_sessions_observed between 1 and 21
      );
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'outcomes_resolved_session_within_horizon'
  ) then
    alter table prod.episode_outcomes add constraint outcomes_resolved_session_within_horizon
      check (
        primary_resolved_session_index is null
        or primary_resolved_session_index between 0 and 20
      );
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- The outcome agrees with the close, or it is not written
-- ---------------------------------------------------------------------------

create or replace function prod.check_outcome_matches_episode()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  e prod.episodes%rowtype;
begin
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
  before insert or update on prod.episode_outcomes
  for each row execute function prod.check_outcome_matches_episode();

-- ---------------------------------------------------------------------------
-- Closing and recording, in one call
-- ---------------------------------------------------------------------------

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
    p_counterfactual_path, coalesce(p_later_target_hit, false),
    p_later_target_hit_at, p_later_target_hit_session_index,
    p_mfe, p_mae, p_sessions_observed,
    coalesce(p_corporate_action_ids, '{}'), p_outcome_currency, p_engine_version,
    p_label_version, p_notes
  );

  return p_episode_id;
end;
$$;

comment on function prod.close_episode_with_outcome is
  'Closes an episode and records its outcome in one call. Two statements in a caller''s transaction would be equivalent when the caller remembers; this makes a closed episode without an outcome unrepresentable rather than merely discouraged.';

revoke all on function prod.close_episode_with_outcome from public;
grant execute on function prod.close_episode_with_outcome to surge_worker_prod;
revoke all on function prod.check_outcome_matches_episode() from public;

-- ---------------------------------------------------------------------------
-- The results contract
-- ---------------------------------------------------------------------------

create or replace view ui.episode_results as
  select e.episode_id,
         e.security_id,
         e.thesis_key,
         e.entry_price_observed_at,
         e.closed_at,
         p.entry_reference_price,
         p.entry_price_currency,
         p.target_price,
         p.initial_failure_line,
         (select r.risk_line
            from prod.risk_line_updates r
           where r.episode_id = e.episode_id
           order by r.effective_at desc, r.update_id desc
           limit 1) as current_risk_line,
         o.primary_episode_outcome::text as primary_outcome,
         o.primary_path_resolution::text as primary_path_resolution,
         o.primary_resolution_granularity as resolution_granularity,
         o.primary_resolved_session_index as resolved_session_index,
         o.primary_detail as resolution_detail,
         o.counterfactual_path_resolution::text as counterfactual_path_resolution,
         o.counterfactual_later_target_hit as later_target_hit,
         o.counterfactual_later_target_hit_session_index as later_target_hit_session_index,
         o.counterfactual_mfe as mfe,
         o.counterfactual_mae as mae,
         o.counterfactual_sessions_observed as sessions_observed,
         o.corporate_action_ids_applied,
         o.outcome_currency,
         o.engine_version,
         o.notes as outcome_notes,
         p.provider_id,
         p.verification::text as verification
    from prod.episodes e
    join prod.predictions p on p.episode_id = e.episode_id
    left join prod.episode_outcomes o on o.episode_id = e.episode_id
   where e.status = 'CLOSED';

comment on view ui.episode_results is
  'One row per finished episode with both layers side by side. The counterfactual columns are named as such so that a later target hit can never be read as the primary answer, and the ambiguity and missing-data reasons are shown rather than folded into a success or a failure.';

grant select on ui.episode_results to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- How the unresolved ones are counted: separately
-- ---------------------------------------------------------------------------

create or replace view ui.outcome_counts as
  select o.primary_episode_outcome::text as outcome,
         count(*) as episodes,
         count(*) filter (where o.counterfactual_later_target_hit) as later_reached_target,
         min(o.counterfactual_mae) as worst_excursion,
         max(o.counterfactual_mfe) as best_excursion
    from prod.episode_outcomes o
   group by o.primary_episode_outcome;

comment on view ui.outcome_counts is
  'AMBIGUOUS_PATH, UNRESOLVED_MISSING_DATA and CORPORATE_ACTION_SUSPECTED are their own rows. Folding them into a hit rate would let "we could not tell" become whichever answer was more convenient (CLAUDE.md 1-9).';

grant select on ui.outcome_counts to surge_web, surge_readonly;
