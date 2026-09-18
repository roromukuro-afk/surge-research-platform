-- The inputs an analysis was made from, durable at TX1.
--
-- What was wrong: the execution row recorded that an analysis had started and
-- what it eventually answered, but not *what it was asked*. The bundle, the
-- guard facts and the thesis lived in the caller's memory, and `resume_all`
-- took them back as arguments. So the durability argument only held while the
-- process that built them was alive - which is the one thing a crash rules out.
-- Recovery would have had to build a *new* bundle from the current session and
-- call that the same analysis.
--
-- It is not the same analysis. An intraday bundle assembled twenty minutes
-- later is a different price, a different tape and possibly a different
-- universe verdict, and an answer to it recorded against the original trigger
-- would be a decision attributed to inputs it never saw.
--
-- So TX1 now stores the exact input, including the rendered prompt's hash,
-- fixed *before* anything is sent. Recovery reads the stored input, rebuilds
-- the prompt and requires the hash to match; a mismatch is terminal
-- (INPUT_RECONSTRUCTION_MISMATCH) rather than something to paper over.
--
-- Also here:
--
--   * analysis_answered_at and decision_completed_at, which the runner's own
--     clock decides. decision_completed_at is not a caller's opinion: it is
--     when this system accepted the answer, after semantic validation.
--   * the entry reference price, which is observed *after* that moment and
--     never before it.
--   * analysis_kind is REANALYSIS on this path. A watch trigger execution that
--     recorded an ENTRY_DECISION attempt would be one event described two ways.
--   * a trigger cannot have happened after the cutoff of the analysis that
--     answers it.
--   * a prediction and its episode must carry the same verification.

-- ---------------------------------------------------------------------------
-- 1. The input snapshot
-- ---------------------------------------------------------------------------

alter table prod.entry_analysis_executions
  --: The decision's own coordinates, as the caller declared them and the
  --: database checked them.
  add column if not exists thesis_key text,
  add column if not exists setup_ids text[] not null default '{}',

  --: The price the model judged against, and the rate that made it comparable
  --: to 3,000 JPY. Without these a resumed analysis cannot re-run the hard
  --: filter on the same numbers.
  add column if not exists decision_price numeric(18, 6),
  add column if not exists decision_price_currency text,
  add column if not exists decision_price_observed_at timestamptz,
  add column if not exists fx_rate numeric(18, 8),
  add column if not exists fx_observed_at timestamptz,
  add column if not exists price_limit_jpy numeric(18, 6),

  --: The verdicts that were true at cutoff. Re-deriving them at recovery would
  --: silently answer a different question.
  add column if not exists universe_decision text,
  add column if not exists universe_reason_code text,
  add column if not exists coverage_meets_requirements boolean,
  add column if not exists coverage_detail text,

  --: The bundle itself, byte for byte as it was serialised - not as jsonb,
  --: which would re-order keys and change the hash it is checked against.
  add column if not exists bundle_serialized text,
  add column if not exists addenda_sha256 text[] not null default '{}',

  --: When the model answered, and when this system accepted the answer.
  add column if not exists analysis_answered_at timestamptz,
  add column if not exists decision_completed_at timestamptz,

  --: Observed after decision_completed_at, and only for an ENTRY.
  add column if not exists entry_reference_price numeric(18, 6),
  add column if not exists entry_price_currency text,
  add column if not exists entry_price_observed_at timestamptz,
  add column if not exists entry_price_method text,
  add column if not exists entry_price_evidence text[] not null default '{}';

comment on column prod.entry_analysis_executions.bundle_serialized is
  'The exact serialised bundle the prompt was rendered from, stored as text so that re-reading it reproduces bundle_sha256 byte for byte. Stored at TX1, before anything is sent.';
comment on column prod.entry_analysis_executions.decision_completed_at is
  'When this system accepted the answer - the model having replied and semantic validation having passed - as measured by the runner''s clock. Never supplied by a caller: it is the moment after which an entry price becomes observable, so a caller-chosen value would let a price from before the decision be used as the price the decision could have traded at.';
comment on column prod.entry_analysis_executions.entry_price_observed_at is
  'Observed strictly after decision_completed_at. A price from before the decision is not a price this decision could have acted on.';

do $$
begin
  --: A trigger execution is a reanalysis. The production watch path has exactly
  --: one entrance - TRIGGER_HIT - and an ENTRY_DECISION recorded against it
  --: would describe the same event two ways: an attempt saying "direct entry"
  --: beside a watch transition saying "reanalysis". A direct-entry path, when
  --: there is one, gets its own entrance rather than borrowing this one.
  if not exists (select 1 from pg_constraint where conname = 'executions_trigger_path_is_reanalysis') then
    alter table prod.entry_analysis_executions
      add constraint executions_trigger_path_is_reanalysis
      check (analysis_kind = 'REANALYSIS');
  end if;

  --: The three clocks, in the only order they can occur in.
  if not exists (select 1 from pg_constraint where conname = 'executions_answer_after_cutoff') then
    alter table prod.entry_analysis_executions
      add constraint executions_answer_after_cutoff
      check (analysis_answered_at is null or analysis_answered_at >= decision_cutoff_at);
  end if;

  if not exists (select 1 from pg_constraint where conname = 'executions_completed_after_answered') then
    alter table prod.entry_analysis_executions
      add constraint executions_completed_after_answered
      check (
        decision_completed_at is null
        or (analysis_answered_at is not null and decision_completed_at >= analysis_answered_at)
      );
  end if;

  --: The whole point of C. An entry price observed before the decision finished
  --: is a price the decision could not have acted on, and using it would make
  --: every entry look luckier than it was.
  if not exists (select 1 from pg_constraint where conname = 'executions_entry_price_after_decision') then
    alter table prod.entry_analysis_executions
      add constraint executions_entry_price_after_decision
      check (
        entry_price_observed_at is null
        or (decision_completed_at is not null and entry_price_observed_at >= decision_completed_at)
      );
  end if;

  --: An entry price is a complete observation or is absent. Half of one would
  --: be a number with no time or no currency attached to it.
  if not exists (select 1 from pg_constraint where conname = 'executions_entry_price_is_whole') then
    alter table prod.entry_analysis_executions
      add constraint executions_entry_price_is_whole
      check (
        (entry_reference_price is null and entry_price_currency is null
         and entry_price_observed_at is null)
        or (entry_reference_price is not null and entry_price_currency is not null
            and entry_price_observed_at is not null)
      );
  end if;

  --: TX1 fixes the input. A row that reached an answer without one could not be
  --: resumed, which is the failure this migration exists to remove.
  if not exists (select 1 from pg_constraint where conname = 'executions_answer_needs_its_input') then
    alter table prod.entry_analysis_executions
      add constraint executions_answer_needs_its_input
      check (
        returned_state is null
        or (bundle_serialized is not null and prompt_sha256 is not null
            and bundle_sha256 is not null and canonical_prompt_sha256 is not null)
      );
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- 2. begin_entry_analysis: store the input, and check the causality
-- ---------------------------------------------------------------------------

drop function if exists prod.begin_entry_analysis(
  uuid, bigint, prod.analysis_kind, timestamptz, text, analysis.provider_kind,
  text, uuid, text, uuid, timestamptz
);

create or replace function prod.begin_entry_analysis(
  p_watch_id uuid,
  p_trigger_transition_id bigint,
  p_analysis_kind prod.analysis_kind,
  p_decision_cutoff_at timestamptz,
  p_provider_id text,
  p_provider_kind analysis.provider_kind,
  p_request_version text,
  p_prompt_sha256 text,
  p_bundle_sha256 text,
  p_canonical_prompt_sha256 text,
  p_bundle_serialized text,
  p_addenda_sha256 text[] default '{}',
  p_thesis_key text default null,
  p_setup_ids text[] default '{}',
  p_decision_price numeric default null,
  p_decision_price_currency text default null,
  p_decision_price_observed_at timestamptz default null,
  p_fx_rate numeric default null,
  p_fx_observed_at timestamptz default null,
  p_price_limit_jpy numeric default null,
  p_universe_decision text default null,
  p_universe_reason_code text default null,
  p_coverage_meets_requirements boolean default null,
  p_coverage_detail text default null,
  p_input_version text default null,
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

  --: This entrance is the watch machine's, so the kind is fixed. Recording an
  --: ENTRY_DECISION here would put an attempt that says "entered directly"
  --: beside a watch transition that says "reanalysis" - one event, two
  --: incompatible descriptions, and nothing to say which is the real one.
  if p_analysis_kind is distinct from 'REANALYSIS' then
    raise exception
      'a watch trigger is answered by a REANALYSIS, not by %; a direct entry path needs its own entrance rather than this one',
      p_analysis_kind;
  end if;

  --: The input has to exist before the model is called, because the point of
  --: storing it is that a process that dies mid-call leaves it behind.
  if p_bundle_serialized is null or p_prompt_sha256 is null or p_bundle_sha256 is null
     or p_canonical_prompt_sha256 is null then
    raise exception
      'an analysis is started from a stored input: bundle %, prompt hash %, bundle hash %, canonical hash %. Without them a crash leaves a row nothing can resume',
      p_bundle_serialized is not null, p_prompt_sha256 is not null,
      p_bundle_sha256 is not null, p_canonical_prompt_sha256 is not null;
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

  --: The cause precedes the analysis of it. A cutoff earlier than the trigger
  --: would mean the bundle was assembled before the thing it is a reaction to,
  --: so the analysis would be answering an event it could not have seen.
  if t.occurred_at > p_decision_cutoff_at then
    raise exception
      'trigger % occurred at %, after the cutoff % of the analysis meant to answer it; the bundle predates the event',
      p_trigger_transition_id, t.occurred_at, p_decision_cutoff_at;
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
    decision_cutoff_at, provider_id, provider_kind, model_id, request_version, run_id,
    prompt_sha256, bundle_sha256, canonical_prompt_sha256, addenda_sha256, bundle_serialized,
    thesis_key, setup_ids, decision_price, decision_price_currency, decision_price_observed_at,
    fx_rate, fx_observed_at, price_limit_jpy,
    universe_decision, universe_reason_code, coverage_meets_requirements, coverage_detail,
    input_version
  ) values (
    p_watch_id, p_trigger_transition_id, w.security_id, p_analysis_kind, 'STARTED',
    p_decision_cutoff_at, p_provider_id, p_provider_kind, p_model_id, p_request_version, p_run_id,
    p_prompt_sha256, p_bundle_sha256, p_canonical_prompt_sha256,
    coalesce(p_addenda_sha256, '{}'), p_bundle_serialized,
    p_thesis_key, coalesce(p_setup_ids, '{}'), p_decision_price, p_decision_price_currency,
    p_decision_price_observed_at, p_fx_rate, p_fx_observed_at, p_price_limit_jpy,
    p_universe_decision, p_universe_reason_code, p_coverage_meets_requirements, p_coverage_detail,
    p_input_version
  )
  returning prod.entry_analysis_executions.analysis_execution_id into new_id;

  return query select new_id, 'STARTED'::prod.analysis_execution_status, true, w.security_id;
end;
$fn$;

comment on function prod.begin_entry_analysis is
  'Moves the watch to IN_REANALYSIS and records the analysis with the exact input it will be run against, in one transaction, before anything is sent. The hashes are fixed here rather than when the answer comes back, so a recovery pass can rebuild the prompt and prove it is rebuilding the same one.';


-- ---------------------------------------------------------------------------
-- 3. The answer, and when it arrived
-- ---------------------------------------------------------------------------

drop function if exists prod.record_entry_analysis_answer(
  uuid, prod.decision_state, text, text, text, text, text, text, text, numeric,
  numeric, numeric, numeric, text[], text, text[], text, text, text
);

create or replace function prod.record_entry_analysis_answer(
  p_analysis_execution_id uuid,
  p_returned_state prod.decision_state,
  p_rationale text,
  p_response_sha256 text,
  p_prompt_sha256 text,
  p_bundle_sha256 text,
  p_canonical_prompt_sha256 text,
  p_analysis_answered_at timestamptz,
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
  p_reject_reason text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  e prod.entry_analysis_executions%rowtype;
begin
  if p_response_sha256 is null or p_analysis_answered_at is null then
    raise exception
      'an answer is stored with its own hash and the time it arrived: response %, answered_at %',
      p_response_sha256 is not null, p_analysis_answered_at is not null;
  end if;

  select * into e from prod.entry_analysis_executions
   where analysis_execution_id = p_analysis_execution_id for update;
  if not found then
    raise exception 'no analysis execution %', p_analysis_execution_id;
  end if;

  --: The hashes were fixed at TX1. They are passed again here and checked
  --: rather than written, because a runner that rendered a different prompt
  --: between starting and asking would otherwise overwrite the record of what
  --: it started with - and the stored input would then describe a request that
  --: produced no answer while the answer described a request nothing stored.
  if e.prompt_sha256 is distinct from p_prompt_sha256
     or e.bundle_sha256 is distinct from p_bundle_sha256
     or e.canonical_prompt_sha256 is distinct from p_canonical_prompt_sha256 then
    raise exception
      'the answer to analysis % was produced from a different request than the one recorded at its start; stored prompt %, bundle %, canonical %',
      p_analysis_execution_id, e.prompt_sha256, e.bundle_sha256, e.canonical_prompt_sha256;
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set returned_state = p_returned_state,
         rationale = p_rationale,
         response_sha256 = p_response_sha256,
         raw_response = p_raw_response,
         raw_response_ref = p_raw_response_ref,
         analysis_answered_at = p_analysis_answered_at,
         decision_price_used = p_decision_price_used,
         initial_failure_line = p_initial_failure_line,
         reachable_zone_low = p_reachable_zone_low,
         reachable_zone_high = p_reachable_zone_high,
         reachable_zone_basis_kinds = coalesce(p_reachable_zone_basis_kinds, '{}'),
         reachable_zone_basis = p_reachable_zone_basis,
         concepts_considered = coalesce(p_concepts_considered, '{}'),
         watch_trigger_description = p_watch_trigger_description,
         reject_reason = p_reject_reason,
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
-- 4. Completion carries the decision's own clock and the entry price
-- ---------------------------------------------------------------------------

drop function if exists prod.complete_entry_analysis(
  uuid, analysis.validation_status, text[], text[], text[], uuid, uuid
);

create or replace function prod.complete_entry_analysis(
  p_analysis_execution_id uuid,
  p_validation_status analysis.validation_status,
  p_validation_errors text[] default '{}',
  p_system_refusals text[] default '{}',
  p_concepts_considered text[] default '{}',
  p_entry_attempt_id uuid default null,
  p_prediction_id uuid default null,
  p_decision_completed_at timestamptz default null,
  p_entry_reference_price numeric default null,
  p_entry_price_currency text default null,
  p_entry_price_observed_at timestamptz default null,
  p_entry_price_method text default null,
  p_entry_price_evidence text[] default '{}'
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

  if e.returned_state is null then
    raise exception
      'analysis % has no stored answer, so there is nothing for it to have completed',
      p_analysis_execution_id;
  end if;

  --: A hosted model's answer is only reproducible with the hashes of what it
  --: was asked. A stand-in never reaches a formal decision, so it is exempt.
  if e.provider_kind = 'HOSTED_LLM'
     and (e.prompt_sha256 is null or e.bundle_sha256 is null
          or e.canonical_prompt_sha256 is null or e.bundle_serialized is null) then
    raise exception
      'analysis % cannot complete without the prompt, bundle and canonical record of what produced it',
      p_analysis_execution_id;
  end if;

  --: The decision's own clock. Recorded here rather than taken from the
  --: caller's facts, so that an entry price can be required to come after it.
  if p_decision_completed_at is not null
     and e.analysis_answered_at is not null
     and p_decision_completed_at < e.analysis_answered_at then
    raise exception
      'analysis % completed at %, before its own answer arrived at %',
      p_analysis_execution_id, p_decision_completed_at, e.analysis_answered_at;
  end if;

  if p_entry_reference_price is not null and p_decision_completed_at is null
     and e.decision_completed_at is null then
    raise exception
      'analysis % recorded an entry price with no decision completion time; there would be nothing to prove the price came after the decision',
      p_analysis_execution_id;
  end if;

  select state into watch_state from prod.watches where watch_id = e.watch_id;
  if watch_state = 'IN_REANALYSIS' then
    raise exception
      'analysis % cannot complete while watch % is still IN_REANALYSIS; the decision has to move the watch in the same transaction',
      p_analysis_execution_id, e.watch_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'on', true);

  update prod.entry_analysis_executions
     set status = 'COMPLETED',
         completed_at = clock_timestamp(),
         validation_status = p_validation_status,
         validation_errors = coalesce(p_validation_errors, '{}'),
         system_refusals = coalesce(p_system_refusals, '{}'),
         concepts_considered = case
           when coalesce(array_length(p_concepts_considered, 1), 0) > 0
             then p_concepts_considered
           else concepts_considered
         end,
         entry_attempt_id = p_entry_attempt_id,
         prediction_id = p_prediction_id,
         decision_completed_at = coalesce(p_decision_completed_at, decision_completed_at),
         entry_reference_price = p_entry_reference_price,
         entry_price_currency = p_entry_price_currency,
         entry_price_observed_at = p_entry_price_observed_at,
         entry_price_method = p_entry_price_method,
         entry_price_evidence = coalesce(p_entry_price_evidence, '{}')
   where analysis_execution_id = p_analysis_execution_id
     and status in ('STARTED', 'RETRY_PENDING');

  if not found then
    perform set_config('prod.writing_analysis_execution', 'off', true);
    raise exception
      'analysis execution % is not open; an analysis has one outcome',
      p_analysis_execution_id;
  end if;

  perform set_config('prod.writing_analysis_execution', 'off', true);
end;
$fn$;


-- ---------------------------------------------------------------------------
-- 5. The guard: the stored input is as immutable as the identity
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

  --: The input is fixed at TX1 and is now immutable rather than write-once.
  --: Write-once was right while the hashes arrived with the answer; now they
  --: arrive with the request, so any change to them is a rewrite of what was
  --: asked - which is precisely the thing a stored input exists to prevent.
  if old.prompt_sha256 is distinct from new.prompt_sha256
     or old.bundle_sha256 is distinct from new.bundle_sha256
     or old.canonical_prompt_sha256 is distinct from new.canonical_prompt_sha256
     or old.addenda_sha256 is distinct from new.addenda_sha256
     or old.bundle_serialized is distinct from new.bundle_serialized
     or old.thesis_key is distinct from new.thesis_key
     or old.setup_ids is distinct from new.setup_ids
     or old.decision_price is distinct from new.decision_price
     or old.decision_price_observed_at is distinct from new.decision_price_observed_at
     or old.fx_rate is distinct from new.fx_rate
     or old.universe_decision is distinct from new.universe_decision then
    raise exception
      'the stored input of analysis execution % is immutable; changing it would describe the answer as coming from a request that was never sent',
      old.analysis_execution_id;
  end if;

  --: The decision's own clock is written once and never moved. Moving it later
  --: would change which prices count as having come after the decision.
  if old.decision_completed_at is not null
     and old.decision_completed_at is distinct from new.decision_completed_at then
    raise exception
      'decision_completed_at of analysis execution % is write-once; moving it would change which prices count as available after the decision',
      old.analysis_execution_id;
  end if;

  return new;
end;
$fn$;

-- ---------------------------------------------------------------------------
-- 6. A prediction and its episode carry the same verification
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
  -- Verification travels the whole way or not at all. A LIVE_VERIFIED
  -- prediction inside an episode nobody verified would be scored as real
  -- evidence, and the episode is the unit scoring counts.
  if e.verification is distinct from new.verification then
    raise exception
      'prediction % is % but episode % is %; verification has to hold for the episode that scores it',
      new.prediction_id, new.verification, e.episode_id, e.verification;
  end if;

  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. The in-flight view carries the input, so recovery can read it
-- ---------------------------------------------------------------------------

drop view if exists ui.entry_analysis_in_flight;
create view ui.entry_analysis_in_flight as
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
         e.bundle_serialized is not null as has_a_stored_input,
         e.attempt_count,
         e.last_transient_error,
         e.retry_not_before,
         e.analysis_answered_at,
         e.decision_completed_at,
         e.entry_price_observed_at,
         w.state::text as watch_state
    from prod.entry_analysis_executions e
    join prod.watches w on w.watch_id = e.watch_id
   where e.status in ('STARTED', 'RETRY_PENDING')
   order by e.started_at;

comment on view ui.entry_analysis_in_flight is
  'Analyses that started and never finished. has_a_stored_input says whether this one can be resumed at all: without it the watch is held at IN_REANALYSIS by a row nothing can act on.';

grant select on ui.entry_analysis_in_flight to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- 8. Grants
--
-- Enumerated from the catalogue rather than written out, because these
-- signatures are long and a mistyped one would revoke nothing and grant
-- nothing while looking exactly like it had.
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
