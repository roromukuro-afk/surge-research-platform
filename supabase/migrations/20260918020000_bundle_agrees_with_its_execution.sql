-- The stored bundle has to agree with the execution it is stored on.
--
-- 20260918010000 made the input durable at TX1 and made recovery prove the
-- rebuilt prompt matches. That proof is strong for a *resumed* analysis and
-- empty for a fresh one: on a fresh run the prompt hash is computed from the
-- same caller-supplied bundle, so it matches itself by construction. What the
-- bundle declares about itself was still taken on trust.
--
-- Python now checks the declarations it can - the canonical and addenda hashes
-- against the texts actually sent, the bundle's cutoff against the facts', the
-- decision price's time against the cutoff - before anything is rendered. This
-- migration adds the half only the database can check, because only the
-- database knows which security the watch is on:
--
--   * the bundle is about the watch's own security,
--   * the bundle was assembled for this analysis's cutoff,
--   * the decision price was observed at or before that cutoff.
--
-- The body is otherwise identical to the previous definition.

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
  stored_bundle jsonb;
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

  --: The stored bundle has to be about this watch's security and this moment.
  --: Python checks the bundle against the texts it will send; only the database
  --: knows which security the watch is on, so that half is checked here. Parsed
  --: as jsonb for comparison only - the stored copy stays text, byte for byte,
  --: because jsonb would re-order keys and change the hash it is checked by.
  begin
    stored_bundle := p_bundle_serialized::jsonb;
  exception when others then
    raise exception
      'the stored bundle for trigger % is not valid JSON, so it cannot be the serialised bundle it claims to be',
      p_trigger_transition_id;
  end;

  if stored_bundle ->> 'security_id' is distinct from w.security_id::text then
    raise exception
      'the stored bundle is about security %, and watch % is on security %; an answer to it would be recorded against the wrong security',
      stored_bundle ->> 'security_id', p_watch_id, w.security_id;
  end if;

  if (stored_bundle ->> 'decision_cutoff_at')::timestamptz is distinct from p_decision_cutoff_at then
    raise exception
      'the stored bundle was assembled for cutoff %, and this analysis is recorded at cutoff %',
      stored_bundle ->> 'decision_cutoff_at', p_decision_cutoff_at;
  end if;

  --: A decision price from after the cutoff is a price from the future of the
  --: decision. The attempt table refuses it too, but only at TX3 - after the
  --: model has already been shown it.
  if p_decision_price_observed_at is not null
     and p_decision_price_observed_at > p_decision_cutoff_at then
    raise exception
      'the decision price was observed at %, after the cutoff %; the model would be shown a price from after the moment it is deciding at',
      p_decision_price_observed_at, p_decision_cutoff_at;
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


do $g$
declare
  fn text;
begin
  for fn in
    select format('%s(%s)', p.oid::regproc, pg_get_function_identity_arguments(p.oid))
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'prod' and p.proname = 'begin_entry_analysis'
  loop
    execute format('revoke all on function %s from public', fn);
    execute format('grant execute on function %s to surge_worker_prod', fn);
  end loop;
end;
$g$;
