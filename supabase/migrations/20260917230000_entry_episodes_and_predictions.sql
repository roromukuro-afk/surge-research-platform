-- Phase 8: watch, entry, prediction, episode.
--
-- This is the schema where the project's rules stop being advice and start
-- being constraints. Everything before it produces analysis; this is where an
-- analysis can become a claim with a score attached, and the difference between
-- those two things is the whole point of the design.
--
-- Four rules are enforced here rather than in application code, because each of
-- them is one careless line away in Python and unrecoverable afterwards:
--
--   1. An end-of-day analysis cannot produce ENTRY. The state is representable
--      here (unlike analysis.stage3_state, which omits it) because an intraday
--      decision genuinely produces it - but only from an ENTRY_DECISION or
--      REANALYSIS analysis.
--   2. A watch cannot become an entry without a reanalysis in between. Reaching
--      a trigger is an observation, not a decision.
--   3. A prediction cannot exist without both prices, both under the limit, in
--      the right order. A missing price is not a small gap to fill in later; it
--      is the absence of the only number the scoring is allowed to use.
--   4. initial_failure_line never changes. current_risk_line lives in another
--      table precisely so that the operational number and the scored number
--      cannot be confused for one another.
--
-- Status: IMPLEMENTED_NOT_LIVE_VERIFIED. No live price provider is settled
-- (D-102 / D-103 / D-06b), so nothing here has run against a real intraday
-- price. The schema, the guards and the pipeline are complete; what is missing
-- is the data, and the guards are written so that missing data fails loudly
-- rather than producing a prediction with a hole in it.

-- ---------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'analysis_kind') then
    create type prod.analysis_kind as enum (
      'EOD',                  -- the end-of-day pass
      'POST_CLOSE_MATERIAL',  -- material that arrived after the close
      'WATCH_MONITOR',        -- intraday monitoring of an open watch
      'ENTRY_DECISION',       -- the intraday decision itself
      'REANALYSIS'            -- a decision taken after a trigger was hit
    );
  end if;
end $$;

comment on type prod.analysis_kind is
  'Which pass produced a record. Only ENTRY_DECISION and REANALYSIS may produce ENTRY: the two end-of-day kinds run against a closed market, where "entry at the current price" has no referent.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'decision_state') then
    create type prod.decision_state as enum (
      'TECHNICAL_SETUP_EOD',
      'POST_CLOSE_CATALYST_SETUP',
      'ENTRY',
      'WATCH_BREAKOUT',
      'WATCH_PULLBACK',
      'WATCH_OTHER',
      'REJECT'
    );
  end if;
end $$;

comment on type prod.decision_state is
  'The seven states of CLAUDE.md 1-4. Unlike analysis.stage3_state this one has ENTRY, because an intraday decision can reach it; the constraint that an end-of-day analysis cannot is enforced per row rather than by leaving the value unrepresentable.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'watch_state') then
    create type prod.watch_state as enum (
      'ARMED',          -- conditions set, nothing has happened
      'TRIGGER_HIT',    -- the condition was reached; an observation, not a decision
      'IN_REANALYSIS',  -- a reanalysis is running
      'ENTERED',        -- the reanalysis said ENTRY and a prediction was made
      'REJECTED',       -- the reanalysis said no
      'REARMED',        -- the reanalysis said keep watching
      'EXPIRED'         -- the watch aged out (D-20)
    );
  end if;
end $$;

comment on type prod.watch_state is
  'The watch machine. TRIGGER_HIT and ENTERED are deliberately not adjacent: CLAUDE.md 1-5 forbids an automatic entry on a trigger, so the path between them runs through IN_REANALYSIS.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'entry_attempt_status') then
    create type prod.entry_attempt_status as enum (
      'PREDICTION_CREATED',
      'ENTRY_ABORTED_PRICE_LIMIT',     -- decision price passed, entry price did not
      'REJECTED_HARD_FILTER_AT_DECISION',
      'REJECTED_BY_ANALYSIS',
      'REJECTED_NOT_IN_UNIVERSE',
      'REAFFIRMED_EXISTING_EPISODE',   -- same security, same thesis, already open
      'NO_ENTRY_REFERENCE_PRICE'       -- D-31: judged ENTRY, could not observe a price
    );
  end if;
end $$;

comment on type prod.entry_attempt_status is
  'Every intraday entry decision leaves one of these, whether or not a prediction followed. An attempt that produced nothing is the most interesting kind of record here, which is why it is a status rather than an absent row.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'episode_status') then
    create type prod.episode_status as enum ('OPEN', 'CLOSED');
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'episode_close_reason') then
    create type prod.episode_close_reason as enum (
      'TARGET_HIT',
      'INITIAL_FAILURE_HIT',
      'THESIS_INVALIDATED',
      'HORIZON_EXPIRED',
      'AMBIGUOUS_PATH',
      'UNRESOLVED_MISSING_DATA',
      'CORPORATE_ACTION_SUSPECTED'
    );
  end if;
end $$;

comment on type prod.episode_close_reason is
  'AMBIGUOUS_PATH and UNRESOLVED_MISSING_DATA are close reasons rather than outcomes on purpose: an episode that could not be resolved is finished with, and counting it as either a success or a failure would be choosing the convenient answer.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'transition_kind') then
    create type prod.transition_kind as enum (
      'EPISODE_OPENED',
      'REAFFIRMED',
      'RISK_LINE_HIT',
      'RISK_LINE_MOVED',
      'THESIS_INVALIDATED',
      'TARGET_HIT',
      'INITIAL_FAILURE_HIT',
      'HORIZON_EXPIRED',
      'EPISODE_CLOSED'
    );
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'verification_status') then
    create type prod.verification_status as enum (
      'LIVE_VERIFIED',
      'IMPLEMENTED_NOT_LIVE_VERIFIED'
    );
  end if;
end $$;

comment on type prod.verification_status is
  'Whether this row was produced against a live provider. The default is the honest one: until a real intraday price source is settled, everything here is IMPLEMENTED_NOT_LIVE_VERIFIED, and that has to be visible in the data rather than only in a report.';

-- ---------------------------------------------------------------------------
-- Setups
-- ---------------------------------------------------------------------------

create table if not exists prod.setups (
  setup_id uuid primary key default gen_random_uuid(),
  security_id uuid not null,
  as_of_date date not null,
  state prod.decision_state not null,
  analysis_kind prod.analysis_kind not null,
  stage3_output_id uuid references analysis.stage3_outputs (output_id),

  --: The close, or whatever price the setup was detected at. Recorded and never
  --: used for scoring: CLAUDE.md 1-4 gives that job to entry_reference_price alone.
  signal_reference_price numeric(18, 6),
  signal_reference_currency text,

  price_cutoff_at timestamptz not null,
  knowledge_cutoff_at timestamptz not null,

  --: A post-close catalyst has not been evaluated against the close and must not
  --: be: the close cannot have priced something published after it.
  priced_in_status text not null default 'EVALUATED_AGAINST_EOD',

  thesis_key text,
  rationale text,
  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),

  constraint setups_state_matches_kind check (
    (analysis_kind = 'EOD' and state <> 'POST_CLOSE_CATALYST_SETUP')
    or (analysis_kind = 'POST_CLOSE_MATERIAL' and state <> 'TECHNICAL_SETUP_EOD')
    or analysis_kind not in ('EOD', 'POST_CLOSE_MATERIAL')
  ),
  -- The rule of CLAUDE.md 1-4, as a constraint rather than a convention.
  constraint setups_eod_never_enters check (
    state <> 'ENTRY' or analysis_kind in ('ENTRY_DECISION', 'REANALYSIS')
  ),
  constraint setups_post_close_is_not_priced_in check (
    state <> 'POST_CLOSE_CATALYST_SETUP' or priced_in_status = 'NOT_EVALUATED_AGAINST_EOD'
  ),
  constraint setups_currency_shape check (
    signal_reference_currency is null or signal_reference_currency ~ '^[A-Z]{3}$'
  ),
  constraint setups_priced_in_vocabulary check (
    priced_in_status in ('EVALUATED_AGAINST_EOD', 'NOT_EVALUATED_AGAINST_EOD')
  ),
  unique (security_id, as_of_date, state, analysis_kind)
);

comment on table prod.setups is
  'One row per setup or watch produced by an end-of-day or post-close pass. A security that is both a technical setup and a post-close catalyst gets two rows, not one merged row, because the next session''s entry decision needs to see them as two different claims.';
comment on column prod.setups.signal_reference_price is
  'Recorded for audit only. The +20% threshold and every outcome are computed from entry_reference_price; using this one would score the prediction against a price nobody could have traded at.';
comment on column prod.setups.priced_in_status is
  'NOT_EVALUATED_AGAINST_EOD for a post-close catalyst. The close cannot have priced in something that had not been published, and the check constraint makes that unforgettable rather than a review comment.';

create index if not exists setups_security_date_idx on prod.setups (security_id, as_of_date desc);

-- ---------------------------------------------------------------------------
-- Watches
-- ---------------------------------------------------------------------------

create table if not exists prod.watches (
  watch_id uuid primary key default gen_random_uuid(),
  setup_id uuid not null references prod.setups (setup_id),
  security_id uuid not null,
  state prod.watch_state not null default 'ARMED',

  trigger_description text not null,
  trigger_price numeric(18, 6),
  trigger_currency text,
  expires_after_date date,

  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  opened_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp()
);

comment on table prod.watches is
  'A condition being watched, and where the machine currently is. The transitions are in prod.watch_transitions; this row is the head of that list and is the only mutable thing in this schema.';

create table if not exists prod.watch_transitions (
  transition_id bigint generated always as identity primary key,
  watch_id uuid not null references prod.watches (watch_id) on delete cascade,
  from_state prod.watch_state,
  to_state prod.watch_state not null,
  occurred_at timestamptz not null,
  observed_price numeric(18, 6),
  observed_price_at timestamptz,
  analysis_kind prod.analysis_kind,
  note text,
  created_at timestamptz not null default clock_timestamp(),

  constraint watch_transitions_no_self_loop check (from_state is distinct from to_state)
);

comment on table prod.watch_transitions is
  'Append-only. Every state the watch has been in, in order, including the ones that went nowhere - a watch that triggered and was then rejected is evidence about the trigger, and deleting it would leave only the triggers that worked.';

create index if not exists watch_transitions_watch_idx on prod.watch_transitions (watch_id, transition_id);

create or replace function prod.check_watch_transition()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  legal boolean;
begin
  -- The one edge that must not exist. CLAUDE.md 1-5: reaching a watch condition
  -- is an observation; entering is a decision, and a decision needs an analysis
  -- between it and the observation.
  if new.from_state = 'TRIGGER_HIT' and new.to_state = 'ENTERED' then
    raise exception
      'a watch cannot go from TRIGGER_HIT straight to ENTERED; a REANALYSIS must run first (watch %)',
      new.watch_id;
  end if;

  legal := case new.from_state
    when null then new.to_state = 'ARMED'
    when 'ARMED' then new.to_state in ('TRIGGER_HIT', 'EXPIRED')
    when 'TRIGGER_HIT' then new.to_state in ('IN_REANALYSIS', 'EXPIRED')
    when 'IN_REANALYSIS' then new.to_state in ('ENTERED', 'REJECTED', 'REARMED', 'EXPIRED')
    when 'REARMED' then new.to_state in ('TRIGGER_HIT', 'EXPIRED')
    when 'REJECTED' then false
    when 'ENTERED' then false
    when 'EXPIRED' then false
  end;

  if not legal then
    raise exception 'illegal watch transition % -> % (watch %)',
      coalesce(new.from_state::text, '(new)'), new.to_state, new.watch_id;
  end if;

  if new.to_state = 'ENTERED' and new.analysis_kind is distinct from 'REANALYSIS' then
    raise exception
      'entering from a watch requires analysis_kind = REANALYSIS, got % (watch %)',
      coalesce(new.analysis_kind::text, 'null'), new.watch_id;
  end if;

  update prod.watches
     set state = new.to_state, updated_at = clock_timestamp()
   where watch_id = new.watch_id;

  return new;
end;
$$;

drop trigger if exists prod_watch_transitions_legal on prod.watch_transitions;
create trigger prod_watch_transitions_legal
  before insert on prod.watch_transitions
  for each row execute function prod.check_watch_transition();

create or replace function prod.forbid_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception '%.% is append-only; record a new row instead of changing %',
    tg_table_schema, tg_table_name, tg_op;
end;
$$;

comment on function prod.forbid_mutation() is
  'Append-only guard for the production record. A prediction that can be edited is not a prediction; it is a note about what someone wishes they had said.';

drop trigger if exists prod_watch_transitions_append_only on prod.watch_transitions;
create trigger prod_watch_transitions_append_only
  before update or delete on prod.watch_transitions
  for each row execute function prod.forbid_mutation();

-- ---------------------------------------------------------------------------
-- Episodes
-- ---------------------------------------------------------------------------

create table if not exists prod.episodes (
  episode_id uuid primary key default gen_random_uuid(),
  security_id uuid not null,
  --: Same security, same thesis, one episode. The definition of "same thesis"
  --: is D-17a and is not settled; until it is, the key is supplied by the caller
  --: and stored verbatim rather than derived from something convenient.
  thesis_key text not null,
  status prod.episode_status not null default 'OPEN',

  opened_at timestamptz not null,
  closed_at timestamptz,
  close_reason prod.episode_close_reason,

  --: S0. Every session index in this episode counts from here, and from nothing
  --: else - not from the setup, not from the watch, not from a reaffirmation.
  entry_price_observed_at timestamptz not null,
  horizon_sessions integer not null default 20,

  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  created_at timestamptz not null default clock_timestamp(),

  constraint episodes_closed_has_reason check (
    (status = 'OPEN' and closed_at is null and close_reason is null)
    or (status = 'CLOSED' and closed_at is not null and close_reason is not null)
  ),
  constraint episodes_horizon_is_twenty check (horizon_sessions = 20)
);

comment on table prod.episodes is
  'One episode per security per thesis, from the first entry to the close. Scoring counts episodes, not predictions: a reaffirmed entry is the same claim stated twice, and counting it twice would make repetition look like accuracy.';
comment on column prod.episodes.horizon_sessions is
  'Fixed at 20 by check constraint. The primary horizon is S20''s close and is not a tunable: making it configurable would let a disappointing result be rescued by a longer window.';
comment on column prod.episodes.entry_price_observed_at is
  'S0. Sessions are counted from the moment the entry price was observed - not from the setup or the watch, and a REAFFIRMED transition does not move it.';

--: At most one open episode per (security, thesis). This is the deduplication
--: rule as an index rather than as a check in the job, because the job that
--: would check it is the one that would be racing with itself.
create unique index if not exists episodes_one_open_per_thesis
  on prod.episodes (security_id, thesis_key)
  where status = 'OPEN';

create index if not exists episodes_security_idx on prod.episodes (security_id, opened_at desc);

-- ---------------------------------------------------------------------------
-- Entry attempts
-- ---------------------------------------------------------------------------

create table if not exists prod.entry_attempts (
  attempt_id uuid primary key default gen_random_uuid(),
  security_id uuid not null,
  setup_id uuid references prod.setups (setup_id),
  watch_id uuid references prod.watches (watch_id),
  status prod.entry_attempt_status not null,
  analysis_kind prod.analysis_kind not null,

  --: The three cutoffs of the lifecycle spec, in the order they must occur.
  decision_cutoff_at timestamptz not null,
  decision_completed_at timestamptz not null,

  --: What the model was looking at.
  decision_price numeric(18, 6),
  decision_price_observed_at timestamptz,
  decision_price_currency text,
  decision_price_jpy numeric(18, 6),
  decision_fx_rate numeric(18, 8),
  decision_fx_observed_at timestamptz,

  --: What could actually have been traded afterwards.
  entry_reference_price numeric(18, 6),
  entry_price_observed_at timestamptz,
  entry_price_method text,
  entry_price_jpy numeric(18, 6),
  entry_fx_rate numeric(18, 8),
  entry_fx_observed_at timestamptz,

  universe_decision universe.decision,
  universe_reason_code text,
  thesis_key text,
  reject_reason text,
  provider_id text,
  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),

  constraint entry_attempts_decision_order check (
    decision_price_observed_at is null
    or (decision_price_observed_at <= decision_cutoff_at
        and decision_cutoff_at <= decision_completed_at)
  ),
  --: The entry price is what was available *after* the decision. A price
  --: observed before the analysis finished is a price the analysis could have
  --: seen, which is a different and much more flattering number.
  constraint entry_attempts_entry_after_decision check (
    entry_price_observed_at is null or entry_price_observed_at >= decision_completed_at
  ),
  constraint entry_attempts_fx_not_after_its_price check (
    (decision_fx_observed_at is null or decision_fx_observed_at <= decision_cutoff_at)
    and (entry_fx_observed_at is null
         or entry_price_observed_at is null
         or entry_fx_observed_at <= entry_price_observed_at)
  ),
  --: The abort has to carry the evidence for itself.
  constraint entry_attempts_abort_shows_the_price check (
    status <> 'ENTRY_ABORTED_PRICE_LIMIT'
    or (entry_price_jpy is not null and entry_price_jpy > 3000)
  ),
  constraint entry_attempts_hard_filter_shows_the_price check (
    status <> 'REJECTED_HARD_FILTER_AT_DECISION'
    or (decision_price_jpy is not null and decision_price_jpy > 3000)
  ),
  constraint entry_attempts_no_price_means_no_price check (
    status <> 'NO_ENTRY_REFERENCE_PRICE' or entry_reference_price is null
  ),
  constraint entry_attempts_only_intraday check (
    analysis_kind in ('ENTRY_DECISION', 'REANALYSIS')
  )
);

comment on table prod.entry_attempts is
  'Every intraday entry decision, including the ones that produced nothing. ENTRY_ABORTED_PRICE_LIMIT and NO_ENTRY_REFERENCE_PRICE are the rows that make the success rate honest: without them the record would only contain the attempts that worked.';
comment on column prod.entry_attempts.entry_price_observed_at is
  'Must be at or after decision_completed_at. A price observed while the analysis was still running is one the analysis might have seen, and scoring against it would be scoring against hindsight.';

create index if not exists entry_attempts_security_idx
  on prod.entry_attempts (security_id, decision_completed_at desc);

-- ---------------------------------------------------------------------------
-- Predictions
-- ---------------------------------------------------------------------------

create table if not exists prod.predictions (
  prediction_id uuid primary key default gen_random_uuid(),
  episode_id uuid not null references prod.episodes (episode_id),
  attempt_id uuid not null unique references prod.entry_attempts (attempt_id),
  security_id uuid not null,
  thesis_key text not null,

  analysis_kind prod.analysis_kind not null,
  state prod.decision_state not null default 'ENTRY',

  --: The only price the scoring may use, and therefore not nullable.
  entry_reference_price numeric(18, 6) not null,
  entry_price_observed_at timestamptz not null,
  entry_price_currency text not null,
  entry_price_method text,
  entry_price_jpy numeric(18, 6) not null,

  --: Recorded so the decision is reproducible; never used for scoring.
  decision_price numeric(18, 6) not null,
  decision_price_observed_at timestamptz not null,
  decision_price_jpy numeric(18, 6) not null,

  --: Fixed here, forever. A moving failure line makes every prediction succeed
  --: eventually, which is why the operational line lives in another table.
  initial_failure_line numeric(18, 6) not null,
  target_price numeric(18, 6) not null,

  data_cutoff timestamptz not null,
  source_setup_ids uuid[] not null default '{}',

  --: Provenance, so a verdict can be traced to the thing that produced it.
  provider_id text not null,
  provider_kind text not null,
  model_id text,
  prompt_sha256 text,
  bundle_sha256 text,
  canonical_prompt_sha256 text,
  rule_version text not null,
  label_version text,

  universe_decision universe.decision not null,
  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),

  --: The 3,000 yen filter, both times, as a constraint. CLAUDE.md 1-4.
  constraint predictions_decision_under_limit check (decision_price_jpy <= 3000),
  constraint predictions_entry_under_limit check (entry_price_jpy <= 3000),
  --: The target is arithmetic on the entry price, not an input.
  constraint predictions_target_is_twenty_percent check (
    abs(target_price - entry_reference_price * 1.20) < 0.000001
  ),
  constraint predictions_failure_is_below_entry check (
    initial_failure_line < entry_reference_price
  ),
  constraint predictions_prices_are_positive check (
    entry_reference_price > 0 and decision_price > 0 and initial_failure_line > 0
  ),
  constraint predictions_time_order check (
    decision_price_observed_at <= data_cutoff
    and data_cutoff <= entry_price_observed_at
  ),
  --: Only an intraday decision creates one of these.
  constraint predictions_only_from_intraday check (
    analysis_kind in ('ENTRY_DECISION', 'REANALYSIS')
  ),
  constraint predictions_state_is_entry check (state = 'ENTRY'),
  constraint predictions_currency_shape check (entry_price_currency ~ '^[A-Z]{3}$'),
  --: A deterministic stand-in exercises the pipeline; it does not produce
  --: claims. CLAUDE.md: mock analysis never becomes a formal prediction.
  constraint predictions_not_from_a_mock check (provider_kind <> 'DETERMINISTIC_MOCK'),
  --: Only a security the universe has decided on. UNRESOLVED is not a synonym
  --: for included (CLAUDE.md 1-10b), so it does not pass here either.
  constraint predictions_universe_included check (universe_decision = 'INCLUDED')
);

comment on table prod.predictions is
  'Append-only. A prediction records what was claimed, when, from which price, against which failure line, by which provider - and none of it can be revised afterwards. Corrections are new episodes, not edits.';
comment on column prod.predictions.initial_failure_line is
  'Fixed at creation and enforced by trigger. The primary outcome, the teacher label and INITIAL_FAILURE_HIT are all judged against this number; prod.risk_line_updates holds the one that may move.';
comment on column prod.predictions.entry_price_jpy is
  'The yen value of the entry price, for the 3,000 yen filter only. For a US security the outcome is computed in USD (CLAUDE.md 1-9); this column decides eligibility and nothing else.';
comment on constraint predictions_not_from_a_mock on prod.predictions is
  'The deterministic stand-in of analysis.llm exists so the pipeline can run without a paid model. Its verdicts are evidence that the plumbing works and no evidence at all about a security.';

create index if not exists predictions_episode_idx on prod.predictions (episode_id, created_at);
create index if not exists predictions_security_idx on prod.predictions (security_id, entry_price_observed_at desc);

drop trigger if exists prod_predictions_append_only on prod.predictions;
create trigger prod_predictions_append_only
  before update or delete on prod.predictions
  for each row execute function prod.forbid_mutation();

drop trigger if exists prod_entry_attempts_append_only on prod.entry_attempts;
create trigger prod_entry_attempts_append_only
  before update or delete on prod.entry_attempts
  for each row execute function prod.forbid_mutation();

--: A prediction may only be made from an attempt that produced one, and the
--: numbers must be the attempt's own. Copying an attempt's decision into a
--: prediction with different prices is the exact failure this catches.
create or replace function prod.check_prediction_matches_attempt()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  a prod.entry_attempts%rowtype;
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
  if a.entry_reference_price is distinct from new.entry_reference_price
     or a.entry_price_observed_at is distinct from new.entry_price_observed_at
     or a.decision_price is distinct from new.decision_price then
    raise exception
      'prediction % does not carry the prices of its attempt %', new.prediction_id, new.attempt_id;
  end if;
  return new;
end;
$$;

drop trigger if exists prod_predictions_match_attempt on prod.predictions;
create trigger prod_predictions_match_attempt
  before insert on prod.predictions
  for each row execute function prod.check_prediction_matches_attempt();

-- ---------------------------------------------------------------------------
-- State transitions and the risk line
-- ---------------------------------------------------------------------------

create table if not exists prod.state_transitions (
  transition_id bigint generated always as identity primary key,
  episode_id uuid not null references prod.episodes (episode_id) on delete restrict,
  kind prod.transition_kind not null,
  occurred_at timestamptz not null,
  session_index integer,
  observed_price numeric(18, 6),
  note text,
  analysis_kind prod.analysis_kind,
  created_at timestamptz not null default clock_timestamp(),

  constraint state_transitions_session_not_before_entry check (
    session_index is null or session_index >= 0
  )
);

comment on table prod.state_transitions is
  'Append-only. Everything that happened to an episode after it opened, including reaffirmations, which are recorded precisely because they must NOT create a second prediction or restart the horizon.';

create index if not exists state_transitions_episode_idx
  on prod.state_transitions (episode_id, transition_id);

drop trigger if exists prod_state_transitions_append_only on prod.state_transitions;
create trigger prod_state_transitions_append_only
  before update or delete on prod.state_transitions
  for each row execute function prod.forbid_mutation();

create table if not exists prod.risk_line_updates (
  update_id bigint generated always as identity primary key,
  episode_id uuid not null references prod.episodes (episode_id) on delete restrict,
  transition_id bigint references prod.state_transitions (transition_id),
  risk_line numeric(18, 6) not null,
  previous_risk_line numeric(18, 6),
  reason text not null,
  effective_at timestamptz not null,
  created_at timestamptz not null default clock_timestamp(),

  constraint risk_line_positive check (risk_line > 0)
);

comment on table prod.risk_line_updates is
  'The operational risk line, which may move. Its first row equals the prediction''s initial_failure_line; every later row is a research record. Touching it never closes an episode - that is what makes it safe to move, and what makes it useless for scoring.';

create index if not exists risk_line_updates_episode_idx
  on prod.risk_line_updates (episode_id, effective_at);

drop trigger if exists prod_risk_line_updates_append_only on prod.risk_line_updates;
create trigger prod_risk_line_updates_append_only
  before update or delete on prod.risk_line_updates
  for each row execute function prod.forbid_mutation();

-- ---------------------------------------------------------------------------
-- Outcomes, in two layers
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'prod' and t.typname = 'path_resolution') then
    create type prod.path_resolution as enum (
      'TARGET_FIRST',
      'FAILURE_FIRST',
      'NEITHER_BY_HORIZON',
      'AMBIGUOUS_PATH',
      'UNRESOLVED_MISSING_DATA'
    );
  end if;
end $$;

create table if not exists prod.episode_outcomes (
  episode_id uuid primary key references prod.episodes (episode_id) on delete restrict,

  --: The formal layer. Stops when the episode stops.
  primary_episode_outcome prod.episode_close_reason,
  primary_path_resolution prod.path_resolution,
  primary_resolution_granularity text,
  primary_resolved_session_index integer,
  primary_resolved_at timestamptz,

  --: The research layer. Keeps going to S20 whatever the episode did, and is
  --: never the answer to "was this prediction right".
  counterfactual_path_resolution prod.path_resolution,
  counterfactual_later_target_hit boolean,
  counterfactual_later_target_hit_at timestamptz,
  counterfactual_mfe numeric(18, 6),
  counterfactual_mae numeric(18, 6),

  corporate_action_ids_applied text[] not null default '{}',
  label_version text,
  computed_at timestamptz not null default clock_timestamp(),

  constraint outcomes_granularity_vocabulary check (
    primary_resolution_granularity is null
    or primary_resolution_granularity in ('SESSION_OPEN', 'DAY', 'INTRADAY_BAR', 'TRADE')
  )
);

comment on table prod.episode_outcomes is
  'Two layers, kept in separate columns so they cannot be confused. A +20% move after THESIS_INVALIDATED belongs to counterfactual_later_target_hit and never to the primary outcome: the thesis was wrong, and the stock rising afterwards does not make it right.';

-- ---------------------------------------------------------------------------
-- Read contracts
-- ---------------------------------------------------------------------------

create or replace view ui.open_episodes as
  select e.episode_id,
         e.security_id,
         e.thesis_key,
         e.opened_at,
         e.entry_price_observed_at,
         e.horizon_sessions,
         p.entry_reference_price,
         p.entry_price_currency,
         p.target_price,
         p.initial_failure_line,
         (select r.risk_line
            from prod.risk_line_updates r
           where r.episode_id = e.episode_id
           order by r.effective_at desc, r.update_id desc
           limit 1) as current_risk_line,
         p.provider_id,
         p.verification::text as verification,
         (select count(*) from prod.state_transitions t where t.episode_id = e.episode_id)
           as transition_count
    from prod.episodes e
    join prod.predictions p on p.episode_id = e.episode_id
   where e.status = 'OPEN';

comment on view ui.open_episodes is
  'Open episodes with both failure lines side by side: the fixed one the prediction is scored against and the movable one used for risk. Showing only one of them would make the other look like it did not exist.';

create or replace view ui.entry_attempt_ledger as
  select a.attempt_id,
         a.security_id,
         a.status::text as status,
         a.analysis_kind::text as analysis_kind,
         a.decision_completed_at,
         a.decision_price_jpy,
         a.entry_price_jpy,
         a.universe_decision::text as universe_decision,
         a.reject_reason,
         a.verification::text as verification,
         (p.prediction_id is not null) as produced_a_prediction
    from prod.entry_attempts a
    left join prod.predictions p on p.attempt_id = a.attempt_id;

comment on view ui.entry_attempt_ledger is
  'Every entry decision and whether it produced anything. The denominator for any honest hit rate, which is why the aborted and rejected attempts are in the same view rather than in a separate one nobody opens.';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table
  prod.setups, prod.watches, prod.watch_transitions, prod.episodes,
  prod.entry_attempts, prod.predictions, prod.state_transitions,
  prod.risk_line_updates, prod.episode_outcomes
from public;

grant select, insert on
  prod.setups, prod.watches, prod.watch_transitions, prod.episodes,
  prod.entry_attempts, prod.predictions, prod.state_transitions,
  prod.risk_line_updates, prod.episode_outcomes
to surge_worker_prod;

--: The watch head row is the one mutable thing here, and the trigger is what
--: moves it; the worker needs the privilege for the trigger to succeed.
grant update (state, updated_at) on prod.watches to surge_worker_prod;
grant update on prod.episode_outcomes to surge_worker_prod;
grant update (status, closed_at, close_reason) on prod.episodes to surge_worker_prod;

grant select on
  prod.setups, prod.watches, prod.watch_transitions, prod.episodes,
  prod.entry_attempts, prod.predictions, prod.state_transitions,
  prod.risk_line_updates, prod.episode_outcomes
to surge_readonly;

grant select on ui.open_episodes, ui.entry_attempt_ledger to surge_web, surge_readonly;

revoke all on function prod.forbid_mutation() from public;
revoke all on function prod.check_watch_transition() from public;
revoke all on function prod.check_prediction_matches_attempt() from public;
