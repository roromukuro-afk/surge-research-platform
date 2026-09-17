-- The intraday analysis, recorded in the database while it happens.
--
-- The claim this replaces was false. The job moved a Python object to
-- IN_REANALYSIS and the docstring said a crash would leave stored state saying
-- an analysis was under way - but nothing was stored. A process that died
-- between the trigger and the answer left a watch sitting at TRIGGER_HIT, which
-- reads as "the trigger was never acted on": precisely the confusion the
-- ordering was supposed to prevent.
--
-- So the transition and the execution row are written together, before the model
-- is called, in one transaction. Three things follow from that, and each is a
-- crash this survives:
--
--   1. commit, then die before calling the model. The watch is IN_REANALYSIS and
--      an execution row says STARTED, so a recovery pass can find it. A runner
--      that only looked for TRIGGER_HIT would walk straight past it.
--   2. get an answer, store it, then die before deciding. The response is on the
--      row; the decision resumes from it rather than paying for the model again
--      and - worse - risking a *different* answer for the same trigger.
--   3. commit a prediction, then die. The idempotency key already has a row, so
--      the retry finds it COMPLETED and stops. Two predictions from one trigger
--      would make repeating yourself look like being right twice.
--
-- The key is (watch_id, trigger_transition_id, analysis_kind). The transition id
-- rather than the watch alone, because a watch legitimately re-arms and gets
-- triggered again, and that second trigger deserves its own analysis.

set search_path = '';

do $mig$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'analysis_execution_status' and n.nspname = 'prod') then
    create type prod.analysis_execution_status as enum (
      'STARTED',    -- committed before the model was called
      'COMPLETED',  -- an answer was stored and the decision ran
      'FAILED'      -- no usable answer; no decision was made
    );
  end if;
end;
$mig$;

create table if not exists prod.entry_analysis_executions (
  analysis_execution_id uuid primary key default gen_random_uuid(),

  watch_id uuid not null references prod.watches (watch_id),
  --: Which trigger this analysis is answering. A re-armed watch that triggers
  --: again is a new analysis, and without this the second one would be refused
  --: as a duplicate of the first.
  trigger_transition_id bigint not null references prod.watch_transitions (transition_id),
  security_id uuid not null references ref.securities (security_id),
  analysis_kind prod.analysis_kind not null,
  status prod.analysis_execution_status not null default 'STARTED',

  started_at timestamptz not null default clock_timestamp(),
  decision_cutoff_at timestamptz not null,

  provider_id text not null,
  provider_kind analysis.provider_kind not null,
  model_id text,

  prompt_sha256 text,
  bundle_sha256 text,
  canonical_prompt_sha256 text,
  request_version text not null,
  input_version text,
  run_id uuid references pipeline.runs (run_id),

  response_sha256 text,
  --: Where the raw answer is, not the answer itself. Object storage holds the
  --: bytes; this holds the pointer and the hash (CLAUDE.md 1-18).
  raw_response_ref text,
  returned_state prod.decision_state,
  rationale text,

  decision_price_used numeric(18, 6),
  initial_failure_line numeric(18, 6),
  reachable_zone_low numeric(18, 6),
  reachable_zone_high numeric(18, 6),
  watch_trigger_description text,

  validation_status analysis.validation_status,
  validation_errors text[] not null default '{}',
  system_refusals text[] not null default '{}',
  warnings text[] not null default '{}',

  entry_attempt_id uuid references prod.entry_attempts (attempt_id),
  prediction_id uuid references prod.predictions (prediction_id),

  completed_at timestamptz,
  failure_class text,
  failure_detail text,

  constraint entry_analysis_execution_is_intraday check (
    analysis_kind in ('ENTRY_DECISION', 'REANALYSIS')
  ),
  constraint entry_analysis_execution_idempotent
    unique (watch_id, trigger_transition_id, analysis_kind),
  constraint entry_analysis_execution_finished check (
    (status = 'STARTED' and completed_at is null)
    or (status <> 'STARTED' and completed_at is not null)
  ),
  constraint entry_analysis_execution_failure_has_a_reason check (
    status <> 'FAILED' or failure_class is not null
  ),
  --: A prediction cannot exist without the attempt that produced it.
  constraint entry_analysis_execution_prediction_needs_attempt check (
    prediction_id is null or entry_attempt_id is not null
  )
);

comment on table prod.entry_analysis_executions is
  'One intraday analysis, from before the model was called to after the decision was made. Written in the same transaction as the TRIGGER_HIT -> IN_REANALYSIS move, so a process that dies mid-analysis leaves a record saying so rather than a watch that looks untouched.';
comment on column prod.entry_analysis_executions.trigger_transition_id is
  'The trigger being answered. Part of the idempotency key: a watch that re-arms and triggers again deserves a new analysis, and keying on the watch alone would refuse it.';
comment on column prod.entry_analysis_executions.status is
  'STARTED means committed before the model was called - that is the state a crash leaves behind, and it is what a recovery pass looks for.';

create index if not exists entry_analysis_executions_in_flight_idx
  on prod.entry_analysis_executions (started_at)
  where status = 'STARTED';

-- ---------------------------------------------------------------------------
-- Append-only, except for the one transition each row is allowed
-- ---------------------------------------------------------------------------

create or replace function prod.guard_entry_analysis_execution()
returns trigger
language plpgsql
set search_path = ''
as $fn$
begin
  if tg_op = 'DELETE' then
    raise exception 'entry analysis executions are append-only; % may not be deleted',
      old.analysis_execution_id;
  end if;

  if coalesce(current_setting('prod.writing_analysis_execution', true), 'off') <> 'on' then
    raise exception
      'prod.entry_analysis_executions is written through prod.complete_entry_analysis() and prod.fail_entry_analysis(); a direct UPDATE would let a finished analysis be rewritten after the fact';
  end if;

  if old.status <> 'STARTED' then
    raise exception
      'analysis execution % is already % and may not be changed again; an analysis has one outcome',
      old.analysis_execution_id, old.status;
  end if;

  --: The identity of the request never moves.
  if old.watch_id is distinct from new.watch_id
     or old.trigger_transition_id is distinct from new.trigger_transition_id
     or old.security_id is distinct from new.security_id
     or old.analysis_kind is distinct from new.analysis_kind
     or old.decision_cutoff_at is distinct from new.decision_cutoff_at
     or old.provider_id is distinct from new.provider_id then
    raise exception
      'the identity of analysis execution % may not be changed; that would attribute an answer to a request that was never made',
      old.analysis_execution_id;
  end if;

  --: The hashes arrive with the answer, so they are write-once rather than
  --: immutable-from-creation: null may become a value, and a value may not
  --: become a different one. Treating them as immutable would refuse the very
  --: update that records what was sent; treating them as freely writable would
  --: let a stored answer be re-attributed to a different prompt afterwards.
  if (old.prompt_sha256 is not null and old.prompt_sha256 is distinct from new.prompt_sha256)
     or (old.bundle_sha256 is not null and old.bundle_sha256 is distinct from new.bundle_sha256)
     or (old.canonical_prompt_sha256 is not null
         and old.canonical_prompt_sha256 is distinct from new.canonical_prompt_sha256) then
    raise exception
      'the prompt, bundle and canonical hashes of analysis execution % are write-once; changing one would re-attribute a stored answer to a request that did not produce it',
      old.analysis_execution_id;
  end if;

  return new;
end;
$fn$;

drop trigger if exists entry_analysis_executions_append_only
  on prod.entry_analysis_executions;
create trigger entry_analysis_executions_append_only
  before update or delete on prod.entry_analysis_executions
  for each row execute function prod.guard_entry_analysis_execution();

-- ---------------------------------------------------------------------------
-- Begin: the transition and the record, together or not at all
-- ---------------------------------------------------------------------------

create or replace function prod.begin_entry_analysis(
  p_watch_id uuid,
  p_trigger_transition_id bigint,
  p_analysis_kind prod.analysis_kind,
  p_security_id uuid,
  p_decision_cutoff_at timestamptz,
  p_provider_id text,
  p_provider_kind analysis.provider_kind,
  p_request_version text,
  p_model_id text default null,
  p_run_id uuid default null,
  p_occurred_at timestamptz default null
)
returns table (analysis_execution_id uuid, status prod.analysis_execution_status, created boolean)
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  existing prod.entry_analysis_executions%rowtype;
  new_id uuid;
begin
  --: Idempotent by the key, and this is what makes a retry safe. A second call
  --: for the same trigger returns what the first one produced instead of
  --: starting a second analysis - and, crucially, without inserting a second
  --: watch transition, which the machine would refuse anyway once the watch has
  --: moved on.
  select * into existing
    from prod.entry_analysis_executions
   where watch_id = p_watch_id
     and trigger_transition_id = p_trigger_transition_id
     and analysis_kind = p_analysis_kind
   for update;

  if found then
    return query select existing.analysis_execution_id, existing.status, false;
    return;
  end if;

  --: The watch moves first. Its own trigger validates the move, so an illegal
  --: one raises here and the execution row is never created - the two are
  --: written together or neither is.
  insert into prod.watch_transitions (watch_id, from_state, to_state, occurred_at, analysis_kind)
  values (
    p_watch_id,
    'TRIGGER_HIT',
    'IN_REANALYSIS',
    coalesce(p_occurred_at, clock_timestamp()),
    p_analysis_kind
  );

  insert into prod.entry_analysis_executions (
    watch_id, trigger_transition_id, security_id, analysis_kind, status,
    decision_cutoff_at, provider_id, provider_kind, model_id, request_version, run_id
  ) values (
    p_watch_id, p_trigger_transition_id, p_security_id, p_analysis_kind, 'STARTED',
    p_decision_cutoff_at, p_provider_id, p_provider_kind, p_model_id, p_request_version, p_run_id
  )
  returning prod.entry_analysis_executions.analysis_execution_id into new_id;

  return query select new_id, 'STARTED'::prod.analysis_execution_status, true;
end;
$fn$;

comment on function prod.begin_entry_analysis is
  'Moves the watch to IN_REANALYSIS and records a STARTED analysis in one transaction, before the model is called. Idempotent on (watch, trigger transition, kind): a retry after a crash returns the existing row rather than starting a second analysis or inserting a second transition.';

-- ---------------------------------------------------------------------------
-- Complete and fail
-- ---------------------------------------------------------------------------

create or replace function prod.complete_entry_analysis(
  p_analysis_execution_id uuid,
  p_returned_state prod.decision_state,
  p_rationale text,
  p_validation_status analysis.validation_status,
  p_prompt_sha256 text default null,
  p_bundle_sha256 text default null,
  p_canonical_prompt_sha256 text default null,
  p_response_sha256 text default null,
  p_raw_response_ref text default null,
  p_decision_price_used numeric default null,
  p_initial_failure_line numeric default null,
  p_reachable_zone_low numeric default null,
  p_reachable_zone_high numeric default null,
  p_watch_trigger_description text default null,
  p_validation_errors text[] default '{}',
  p_system_refusals text[] default '{}',
  p_warnings text[] default '{}',
  p_entry_attempt_id uuid default null,
  p_prediction_id uuid default null,
  p_input_version text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'COMPLETED',
         completed_at = clock_timestamp(),
         returned_state = p_returned_state,
         rationale = p_rationale,
         validation_status = p_validation_status,
         prompt_sha256 = coalesce(p_prompt_sha256, prompt_sha256),
         bundle_sha256 = coalesce(p_bundle_sha256, bundle_sha256),
         canonical_prompt_sha256 = coalesce(p_canonical_prompt_sha256, canonical_prompt_sha256),
         response_sha256 = p_response_sha256,
         raw_response_ref = p_raw_response_ref,
         decision_price_used = p_decision_price_used,
         initial_failure_line = p_initial_failure_line,
         reachable_zone_low = p_reachable_zone_low,
         reachable_zone_high = p_reachable_zone_high,
         watch_trigger_description = p_watch_trigger_description,
         validation_errors = coalesce(p_validation_errors, '{}'),
         system_refusals = coalesce(p_system_refusals, '{}'),
         warnings = coalesce(p_warnings, '{}'),
         entry_attempt_id = p_entry_attempt_id,
         prediction_id = p_prediction_id,
         input_version = coalesce(p_input_version, input_version)
   where analysis_execution_id = p_analysis_execution_id;

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

create or replace function prod.fail_entry_analysis(
  p_analysis_execution_id uuid,
  p_failure_class text,
  p_failure_detail text default null,
  p_validation_status analysis.validation_status default null,
  p_validation_errors text[] default '{}',
  p_response_sha256 text default null,
  p_raw_response_ref text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  if p_failure_class is null or btrim(p_failure_class) = '' then
    raise exception 'a failed analysis has to say what kind of failure it was';
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'FAILED',
         completed_at = clock_timestamp(),
         failure_class = p_failure_class,
         failure_detail = p_failure_detail,
         validation_status = p_validation_status,
         validation_errors = coalesce(p_validation_errors, '{}'),
         response_sha256 = p_response_sha256,
         raw_response_ref = p_raw_response_ref
   where analysis_execution_id = p_analysis_execution_id;

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

-- ---------------------------------------------------------------------------
-- Recovery
-- ---------------------------------------------------------------------------

create or replace view ui.entry_analysis_in_flight as
  select e.analysis_execution_id,
         e.watch_id,
         e.trigger_transition_id,
         e.security_id,
         e.analysis_kind::text as analysis_kind,
         e.started_at,
         e.decision_cutoff_at,
         e.provider_id,
         e.response_sha256 is not null as has_a_stored_answer,
         w.state::text as watch_state
    from prod.entry_analysis_executions e
    join prod.watches w on w.watch_id = e.watch_id
   where e.status = 'STARTED'
   order by e.started_at;

comment on view ui.entry_analysis_in_flight is
  'Analyses that were started and never finished. A recovery pass reads this rather than looking for watches at TRIGGER_HIT: by the time the model is called the watch has already moved to IN_REANALYSIS, so a TRIGGER_HIT-only sweep walks past exactly the cases that crashed. has_a_stored_answer says whether the model was already paid for.';

grant select on ui.entry_analysis_in_flight to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table prod.entry_analysis_executions from public;
grant select on prod.entry_analysis_executions
  to surge_worker_prod, surge_worker_research, surge_readonly;

--: The worker inserts nothing directly and updates nothing directly. Beginning,
--: completing and failing all go through the functions, which is what keeps the
--: transition and the record in step.
revoke insert, update, delete on prod.entry_analysis_executions from surge_worker_prod;

revoke all on function prod.begin_entry_analysis(
  uuid, bigint, prod.analysis_kind, uuid, timestamptz, text, analysis.provider_kind, text, text,
  uuid, timestamptz
) from public;
revoke all on function prod.complete_entry_analysis(
  uuid, prod.decision_state, text, analysis.validation_status, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text, text[], text[], text[], uuid, uuid, text
) from public;
revoke all on function prod.fail_entry_analysis(
  uuid, text, text, analysis.validation_status, text[], text, text
) from public;

grant execute on function prod.begin_entry_analysis(
  uuid, bigint, prod.analysis_kind, uuid, timestamptz, text, analysis.provider_kind, text, text,
  uuid, timestamptz
) to surge_worker_prod;
grant execute on function prod.complete_entry_analysis(
  uuid, prod.decision_state, text, analysis.validation_status, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text, text[], text[], text[], uuid, uuid, text
) to surge_worker_prod;
grant execute on function prod.fail_entry_analysis(
  uuid, text, text, analysis.validation_status, text[], text, text
) to surge_worker_prod;

-- ---------------------------------------------------------------------------
-- Record the answer, without finishing the analysis
-- ---------------------------------------------------------------------------
--
-- The second crash case needs its own step. Storing the model's answer and
-- making the decision are not one action: a process can die between them, and
-- when it does the answer has already been paid for. Resuming from it is not
-- only cheaper - a second call could return something different, and then which
-- answer was *the* analysis for this trigger would have no answer.
--
-- The row stays STARTED, because nothing has been decided yet.

create or replace function prod.record_entry_analysis_answer(
  p_analysis_execution_id uuid,
  p_returned_state prod.decision_state,
  p_rationale text,
  p_response_sha256 text,
  p_raw_response_ref text default null,
  p_prompt_sha256 text default null,
  p_bundle_sha256 text default null,
  p_canonical_prompt_sha256 text default null,
  p_decision_price_used numeric default null,
  p_initial_failure_line numeric default null,
  p_reachable_zone_low numeric default null,
  p_reachable_zone_high numeric default null,
  p_watch_trigger_description text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set returned_state = p_returned_state,
         rationale = p_rationale,
         response_sha256 = p_response_sha256,
         raw_response_ref = p_raw_response_ref,
         prompt_sha256 = coalesce(p_prompt_sha256, prompt_sha256),
         bundle_sha256 = coalesce(p_bundle_sha256, bundle_sha256),
         canonical_prompt_sha256 = coalesce(p_canonical_prompt_sha256, canonical_prompt_sha256),
         decision_price_used = p_decision_price_used,
         initial_failure_line = p_initial_failure_line,
         reachable_zone_low = p_reachable_zone_low,
         reachable_zone_high = p_reachable_zone_high,
         watch_trigger_description = p_watch_trigger_description
   where analysis_execution_id = p_analysis_execution_id;

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

comment on function prod.record_entry_analysis_answer is
  'Stores the model answer and leaves the analysis STARTED. A crash after this resumes from the stored answer rather than asking again: a second call could return something different, and then which one was the analysis for this trigger would have no answer.';

revoke all on function prod.record_entry_analysis_answer(
  uuid, prod.decision_state, text, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text
) from public;
grant execute on function prod.record_entry_analysis_answer(
  uuid, prod.decision_state, text, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text
) to surge_worker_prod;
