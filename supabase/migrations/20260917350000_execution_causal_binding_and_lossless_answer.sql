-- Four holes that a real credential would have turned into real damage.
--
-- **The caller chose its own facts.** begin_entry_analysis took watch_id,
-- trigger_transition_id and security_id as three independent arguments and
-- believed all three. A transition belonging to a different watch, a trigger
-- that was answered last week, or a security that is not the watch's would all
-- have been accepted. Now the watch is locked, the transition is read and
-- checked against it, and the security is *derived* rather than trusted.
--
-- **The stored answer was lossy.** reachable_zone_basis_kinds and
-- reachable_zone_basis were not stored at all - and the entry validator
-- *requires* them for an ENTRY. So a process that crashed after the model
-- answered would resume from a reconstruction that could no longer pass the
-- contract the original answer passed. Every semantic field is stored now, plus
-- the raw response.
--
-- **The hashes were columns nobody filled.** Which prompt and which bundle an
-- answer belongs to is the whole of reproducibility (CLAUDE.md 1-18), and the
-- store was passing null. They are required at completion for a hosted model.
--
-- **A provider timeout stranded the watch.** Any failure marked the execution
-- FAILED and left the watch at IN_REANALYSIS forever: no recovery pass looks at
-- a finished execution, and no other code path moves a watch out of
-- IN_REANALYSIS. A network blip became a permanently stuck security. Transient
-- and terminal are now different things, and a terminal failure has to say where
-- the watch went.

set search_path = '';

-- ---------------------------------------------------------------------------
-- 1. RETRY_PENDING, and the columns the answer needs
-- ---------------------------------------------------------------------------

drop function if exists prod.begin_entry_analysis(
  uuid, bigint, prod.analysis_kind, uuid, timestamptz, text, analysis.provider_kind, text, text,
  uuid, timestamptz
);
drop function if exists prod.complete_entry_analysis(
  uuid, prod.decision_state, text, analysis.validation_status, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text, text[], text[], text[], uuid, uuid, text
);
drop function if exists prod.fail_entry_analysis(
  uuid, text, text, analysis.validation_status, text[], text, text
);
drop function if exists prod.record_entry_analysis_answer(
  uuid, prod.decision_state, text, text, text, text, text, text,
  numeric, numeric, numeric, numeric, text
);

--: The view and the partial index both read status, so both depend on the type
--: and either would block the column's type change. Dropped here, recreated
--: below against the new one.
drop view if exists ui.entry_analysis_in_flight;
drop index if exists prod.entry_analysis_executions_in_flight_idx;

--: And the two check constraints, whose literals are bound to the old type.
--: `finished` has to change anyway: RETRY_PENDING is open, so it has no
--: completed_at either, and the old "anything that is not STARTED is finished"
--: reading would have demanded one.
alter table prod.entry_analysis_executions
  drop constraint if exists entry_analysis_execution_finished,
  drop constraint if exists entry_analysis_execution_failure_has_a_reason;

do $mig$
begin
  if not exists (
    select 1 from pg_enum e
    join pg_type t on t.oid = e.enumtypid
    join pg_namespace n on n.oid = t.typnamespace
    where n.nspname = 'prod' and t.typname = 'analysis_execution_status'
      and e.enumlabel = 'RETRY_PENDING'
  ) then
    alter table prod.entry_analysis_executions alter column status drop default;
    alter type prod.analysis_execution_status rename to analysis_execution_status_old;
    create type prod.analysis_execution_status as enum (
      'STARTED',        -- committed before the model was called
      'RETRY_PENDING',  -- a transient failure; the same execution may be retried
      'COMPLETED',      -- an answer was stored and the decision ran
      'FAILED'          -- terminal; no decision was made and none will be
    );
    alter table prod.entry_analysis_executions
      alter column status type prod.analysis_execution_status
      using status::text::prod.analysis_execution_status;
    alter table prod.entry_analysis_executions alter column status set default 'STARTED';
    drop type prod.analysis_execution_status_old;
  end if;
end;
$mig$;

alter table prod.entry_analysis_executions
  add column if not exists reachable_zone_basis_kinds text[] not null default '{}',
  add column if not exists reachable_zone_basis text,
  add column if not exists concepts_considered text[] not null default '{}',
  add column if not exists reject_reason text,
  --: The answer as it arrived. The parsed columns are what the database can be
  --: queried on; this is what proves they were parsed from something real.
  add column if not exists raw_response text,
  add column if not exists attempt_count integer not null default 0,
  add column if not exists last_transient_error text,
  add column if not exists retry_not_before timestamptz;

comment on column prod.entry_analysis_executions.reachable_zone_basis_kinds is
  'Stored because the entry validator requires it for an ENTRY. Dropping it would mean a resumed analysis could no longer pass the contract the original answer passed.';
comment on column prod.entry_analysis_executions.raw_response is
  'The answer as it arrived, beside the parsed columns. A reconstruction that disagreed with the raw text would otherwise be invisible.';
comment on column prod.entry_analysis_executions.status is
  'STARTED is committed-before-the-model-was-called. RETRY_PENDING is a transient failure the same execution may retry. COMPLETED and FAILED are terminal, and a FAILED row must leave the watch somewhere other than IN_REANALYSIS.';

-- ---------------------------------------------------------------------------
-- 2. begin: derive what can be derived, verify the rest
-- ---------------------------------------------------------------------------

create or replace function prod.begin_entry_analysis(
  p_watch_id uuid,
  p_trigger_transition_id bigint,
  p_analysis_kind prod.analysis_kind,
  p_decision_cutoff_at timestamptz,
  p_provider_id text,
  p_provider_kind analysis.provider_kind,
  p_request_version text,
  p_security_id uuid default null,
  p_model_id text default null,
  p_run_id uuid default null,
  p_occurred_at timestamptz default null
)
returns table (
  analysis_execution_id uuid,
  status prod.analysis_execution_status,
  created boolean,
  security_id uuid
)
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  existing prod.entry_analysis_executions%rowtype;
  w prod.watches%rowtype;
  t prod.watch_transitions%rowtype;
  later_trigger bigint;
  new_id uuid;
begin
  --: Idempotent first, and before the watch is locked. A completed analysis is
  --: an answer to this call whatever state the watch is in - and after a crash
  --: the watch is never where it started.
  select * into existing
    from prod.entry_analysis_executions
   where watch_id = p_watch_id
     and trigger_transition_id = p_trigger_transition_id
     and analysis_kind = p_analysis_kind
   for update;

  if found then
    return query select existing.analysis_execution_id, existing.status, false, existing.security_id;
    return;
  end if;

  --: Locked, so two runners cannot both decide this trigger is unanswered.
  select * into w from prod.watches where watch_id = p_watch_id for update;
  if not found then
    raise exception 'watch % does not exist', p_watch_id;
  end if;

  select * into t from prod.watch_transitions where transition_id = p_trigger_transition_id;
  if not found then
    raise exception 'watch transition % does not exist', p_trigger_transition_id;
  end if;

  --: The three arguments have to describe one event. Believing them separately
  --: is what would let another watch's trigger start an analysis here.
  if t.watch_id is distinct from p_watch_id then
    raise exception
      'transition % belongs to watch %, not %; a trigger from another watch cannot start this analysis',
      p_trigger_transition_id, t.watch_id, p_watch_id;
  end if;

  if t.to_state is distinct from 'TRIGGER_HIT' then
    raise exception
      'transition % moved the watch to %, not TRIGGER_HIT; only a trigger can be answered by a reanalysis',
      p_trigger_transition_id, t.to_state;
  end if;

  if w.state is distinct from 'TRIGGER_HIT' then
    raise exception
      'watch % is in state %, not TRIGGER_HIT; there is no unanswered trigger to reanalyse',
      p_watch_id, w.state;
  end if;

  --: The trigger being answered has to be the *current* one. A watch that
  --: re-armed and triggered again has a newer trigger, and answering the old
  --: one would attach this analysis to an event that has already been closed
  --: out - the timestamps would look right and the causality would be wrong.
  select max(transition_id) into later_trigger
    from prod.watch_transitions
   where watch_id = p_watch_id and to_state = 'TRIGGER_HIT';

  if later_trigger is distinct from p_trigger_transition_id then
    raise exception
      'transition % is not the watch''s current trigger (% is); an earlier trigger has already been superseded',
      p_trigger_transition_id, later_trigger;
  end if;

  --: Derived, not trusted. If the caller supplied one it has to agree.
  if p_security_id is not null and p_security_id is distinct from w.security_id then
    raise exception
      'the caller says security %, and watch % is on security %',
      p_security_id, p_watch_id, w.security_id;
  end if;

  insert into prod.watch_transitions (watch_id, from_state, to_state, occurred_at, analysis_kind)
  values (
    p_watch_id, 'TRIGGER_HIT', 'IN_REANALYSIS',
    coalesce(p_occurred_at, clock_timestamp()), p_analysis_kind
  );

  insert into prod.entry_analysis_executions (
    watch_id, trigger_transition_id, security_id, analysis_kind, status,
    decision_cutoff_at, provider_id, provider_kind, model_id, request_version, run_id
  ) values (
    p_watch_id, p_trigger_transition_id, w.security_id, p_analysis_kind, 'STARTED',
    p_decision_cutoff_at, p_provider_id, p_provider_kind, p_model_id, p_request_version, p_run_id
  )
  returning prod.entry_analysis_executions.analysis_execution_id into new_id;

  return query select new_id, 'STARTED'::prod.analysis_execution_status, true, w.security_id;
end;
$fn$;

comment on function prod.begin_entry_analysis is
  'Moves the watch to IN_REANALYSIS and records a STARTED analysis in one transaction, before the model is called. Locks the watch, verifies that the trigger transition belongs to it, is a TRIGGER_HIT, and is the current one, and derives the security from the watch rather than believing the caller.';

-- ---------------------------------------------------------------------------
-- 3. The answer, in full
-- ---------------------------------------------------------------------------

create or replace function prod.record_entry_analysis_answer(
  p_analysis_execution_id uuid,
  p_returned_state prod.decision_state,
  p_rationale text,
  p_response_sha256 text,
  p_prompt_sha256 text,
  p_bundle_sha256 text,
  p_canonical_prompt_sha256 text,
  p_raw_response text default null,
  p_raw_response_ref text default null,
  p_decision_price_used numeric default null,
  p_initial_failure_line numeric default null,
  p_reachable_zone_low numeric default null,
  p_reachable_zone_high numeric default null,
  p_reachable_zone_basis_kinds text[] default '{}',
  p_reachable_zone_basis text default null,
  p_concepts_considered text[] default '{}',
  p_watch_trigger_description text default null,
  p_reject_reason text default null,
  p_input_version text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  if p_response_sha256 is null or p_prompt_sha256 is null or p_bundle_sha256 is null
     or p_canonical_prompt_sha256 is null then
    raise exception
      'an answer is stored with the hashes of what produced it: response %, prompt %, bundle %, canonical %. Without them the stored answer belongs to no particular request',
      p_response_sha256 is not null, p_prompt_sha256 is not null,
      p_bundle_sha256 is not null, p_canonical_prompt_sha256 is not null;
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set returned_state = p_returned_state,
         rationale = p_rationale,
         response_sha256 = p_response_sha256,
         raw_response = p_raw_response,
         raw_response_ref = p_raw_response_ref,
         prompt_sha256 = coalesce(p_prompt_sha256, prompt_sha256),
         bundle_sha256 = coalesce(p_bundle_sha256, bundle_sha256),
         canonical_prompt_sha256 = coalesce(p_canonical_prompt_sha256, canonical_prompt_sha256),
         decision_price_used = p_decision_price_used,
         initial_failure_line = p_initial_failure_line,
         reachable_zone_low = p_reachable_zone_low,
         reachable_zone_high = p_reachable_zone_high,
         reachable_zone_basis_kinds = coalesce(p_reachable_zone_basis_kinds, '{}'),
         reachable_zone_basis = p_reachable_zone_basis,
         concepts_considered = coalesce(p_concepts_considered, '{}'),
         watch_trigger_description = p_watch_trigger_description,
         reject_reason = p_reject_reason,
         input_version = coalesce(p_input_version, input_version),
         status = 'STARTED',
         last_transient_error = null,
         retry_not_before = null
   where analysis_execution_id = p_analysis_execution_id
     and status in ('STARTED', 'RETRY_PENDING');

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception
      'no analysis execution % that is still open; an answer cannot be attached to a finished analysis',
      p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

-- ---------------------------------------------------------------------------
-- 4. Completion, with the hashes required and the watch checked
-- ---------------------------------------------------------------------------

create or replace function prod.complete_entry_analysis(
  p_analysis_execution_id uuid,
  p_validation_status analysis.validation_status,
  p_validation_errors text[] default '{}',
  p_system_refusals text[] default '{}',
  p_warnings text[] default '{}',
  p_entry_attempt_id uuid default null,
  p_prediction_id uuid default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  e prod.entry_analysis_executions%rowtype;
  watch_state prod.watch_state;
begin
  select * into e from prod.entry_analysis_executions
   where analysis_execution_id = p_analysis_execution_id for update;
  if not found then
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  --: A hosted model's answer without the hashes of what produced it cannot be
  --: reproduced or audited (CLAUDE.md 1-18). The stand-in is exempt because it
  --: never reaches a decision anyway.
  if e.provider_kind <> 'DETERMINISTIC_MOCK'
     and (e.prompt_sha256 is null or e.bundle_sha256 is null
          or e.canonical_prompt_sha256 is null or e.response_sha256 is null) then
    raise exception
      'analysis execution % cannot be completed without the prompt, bundle, canonical and response hashes; the answer would belong to no particular request',
      p_analysis_execution_id;
  end if;

  select state into watch_state from prod.watches where watch_id = e.watch_id;
  if watch_state = 'IN_REANALYSIS' then
    raise exception
      'watch % is still IN_REANALYSIS; an analysis is completed after the watch has been moved to where the decision put it, not before',
      e.watch_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'COMPLETED',
         completed_at = clock_timestamp(),
         validation_status = p_validation_status,
         validation_errors = coalesce(p_validation_errors, '{}'),
         system_refusals = coalesce(p_system_refusals, '{}'),
         warnings = coalesce(p_warnings, '{}'),
         entry_attempt_id = p_entry_attempt_id,
         prediction_id = p_prediction_id
   where analysis_execution_id = p_analysis_execution_id;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

-- ---------------------------------------------------------------------------
-- 5. Transient and terminal are different failures
-- ---------------------------------------------------------------------------

create or replace function prod.retry_entry_analysis(
  p_analysis_execution_id uuid,
  p_transient_error text,
  p_retry_not_before timestamptz default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'RETRY_PENDING',
         attempt_count = attempt_count + 1,
         last_transient_error = p_transient_error,
         retry_not_before = p_retry_not_before
   where analysis_execution_id = p_analysis_execution_id
     and status in ('STARTED', 'RETRY_PENDING');

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception 'no open analysis execution %', p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

comment on function prod.retry_entry_analysis is
  'A transient provider or network failure. The execution stays open and the watch stays IN_REANALYSIS, because it genuinely is: the same analysis will be tried again. Marking this FAILED would strand the watch, since nothing moves a watch out of IN_REANALYSIS except the decision that never came.';

create or replace function prod.fail_entry_analysis(
  p_analysis_execution_id uuid,
  p_failure_class text,
  p_failure_detail text default null,
  p_validation_status analysis.validation_status default null,
  p_validation_errors text[] default '{}'
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  e prod.entry_analysis_executions%rowtype;
  watch_state prod.watch_state;
begin
  if p_failure_class is null or btrim(p_failure_class) = '' then
    raise exception 'a failed analysis has to say what kind of failure it was';
  end if;

  select * into e from prod.entry_analysis_executions
   where analysis_execution_id = p_analysis_execution_id for update;
  if not found then
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  select state into watch_state from prod.watches where watch_id = e.watch_id;
  if watch_state = 'IN_REANALYSIS' then
    raise exception
      'watch % would be left IN_REANALYSIS by failing analysis %. Nothing else moves a watch out of that state, so the security would be stuck forever. Move the watch to REARMED, REJECTED or EXPIRED in this transaction, or use prod.retry_entry_analysis for a transient failure',
      e.watch_id, p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'FAILED',
         completed_at = clock_timestamp(),
         failure_class = p_failure_class,
         failure_detail = p_failure_detail,
         validation_status = p_validation_status,
         validation_errors = coalesce(p_validation_errors, '{}')
   where analysis_execution_id = p_analysis_execution_id;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;

-- ---------------------------------------------------------------------------
-- 6. The guard, and what an open execution now means
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
      'prod.entry_analysis_executions is written through its functions; a direct UPDATE would let a finished analysis be rewritten after the fact';
  end if;

  if old.status in ('COMPLETED', 'FAILED') then
    raise exception
      'analysis execution % is already % and may not be changed again; an analysis has one outcome',
      old.analysis_execution_id, old.status;
  end if;

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

alter table prod.entry_analysis_executions
  add constraint entry_analysis_execution_finished check (
    (status in ('STARTED', 'RETRY_PENDING') and completed_at is null)
    or (status in ('COMPLETED', 'FAILED') and completed_at is not null)
  ),
  add constraint entry_analysis_execution_failure_has_a_reason check (
    status <> 'FAILED' or failure_class is not null
  ),
  --: A retry has to say what went wrong, or it is indistinguishable from an
  --: execution that simply has not been picked up yet.
  add constraint entry_analysis_execution_retry_has_a_reason check (
    status <> 'RETRY_PENDING' or last_transient_error is not null
  );

create index if not exists entry_analysis_executions_open_idx
  on prod.entry_analysis_executions (started_at)
  where status in ('STARTED', 'RETRY_PENDING');

create or replace view ui.entry_analysis_in_flight as
  select e.analysis_execution_id,
         e.watch_id,
         e.trigger_transition_id,
         e.security_id,
         e.analysis_kind::text as analysis_kind,
         e.status::text as status,
         e.started_at,
         e.decision_cutoff_at,
         e.provider_id,
         e.response_sha256 is not null as has_a_stored_answer,
         e.attempt_count,
         e.last_transient_error,
         e.retry_not_before,
         w.state::text as watch_state
    from prod.entry_analysis_executions e
    join prod.watches w on w.watch_id = e.watch_id
   where e.status in ('STARTED', 'RETRY_PENDING')
   order by e.started_at;

comment on view ui.entry_analysis_in_flight is
  'Analyses that were started and never finished, including the ones waiting on a retry. A recovery pass reads this rather than looking for watches at TRIGGER_HIT: by the time the model is called the watch has already moved to IN_REANALYSIS, so a TRIGGER_HIT-only sweep walks past exactly the cases that crashed.';

grant select on ui.entry_analysis_in_flight to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- 7. Grants
-- ---------------------------------------------------------------------------

do $g$
declare
  fn text;
begin
  for fn in
    select format('%s(%s)', p.oid::regproc, pg_get_function_identity_arguments(p.oid))
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'prod'
       and p.proname in ('begin_entry_analysis', 'record_entry_analysis_answer',
                         'complete_entry_analysis', 'fail_entry_analysis',
                         'retry_entry_analysis')
  loop
    execute format('revoke all on function %s from public', fn);
    execute format('grant execute on function %s to surge_worker_prod', fn);
  end loop;
end;
$g$;
