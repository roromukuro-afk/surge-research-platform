-- EOD predictions (D-261 / D-267): made after the target session's close is
-- confirmed, and referenced to that close.
--
-- A table of its own rather than new rows in prod.predictions. That table's
-- constraints encode the intraday entry - a price observed after the decision,
-- an attempt and an episode it must agree with - and are kept, unrelaxed, for
-- the future execution study (D-263). Nothing here loosens them.
--
-- The rules are the user's decision of 2026-09-18
-- (docs/requirements/user-decision-2026-09-18-eod-prediction-rules.original.txt),
-- specified in docs/specs/eod-prediction.md (eod-prediction-1.0.0):
--   S0 = the target session; predict after its close is confirmed; the
--   reference is its confirmed close; the 3,000 yen filter is judged on it;
--   target = close x 1.20; evaluation starts at S1; checkpoints at T+1, T+3,
--   T+5, T+10 and T+20; the last is the deadline.

create table if not exists prod.eod_predictions (
  eod_prediction_id uuid primary key default gen_random_uuid(),
  security_id uuid not null,
  market_code text not null,
  thesis_key text not null,

  --: S0 and its confirmed close: signal_reference_price.
  s0_session_date date not null,
  signal_reference_price numeric(18, 6) not null,
  signal_price_currency text not null,
  signal_price_jpy numeric(18, 6) not null,
  fx_rate numeric(18, 8),
  fx_observed_at timestamptz,

  --: When the close could no longer change, and where and when it was read.
  s0_session_closed_at timestamptz not null,
  close_fetched_at timestamptz not null,
  close_publication_delay_seconds integer not null,
  close_provider text not null,
  close_feed text not null,
  close_basis text not null,
  close_evidence text[] not null default '{}',

  data_cutoff timestamptz not null,
  decision_completed_at timestamptz not null,

  --: Arithmetic on the close, not an input.
  target_price numeric(18, 6) not null,
  --: Fixed here when the analysis gives one (CLAUDE.md 1-6). Whether an EOD
  --: prediction must have one is an Outcome-side rule, so NULL is allowed for now.
  initial_failure_line numeric(18, 6),
  evaluation_start_session smallint not null default 1,
  checkpoint_sessions smallint[] not null default '{1,3,5,10,20}',
  horizon_sessions smallint not null default 20,

  --: Provenance, so a verdict can be traced to the thing that produced it.
  provider_id text not null,
  provider_kind text not null,
  model_id text,
  prompt_sha256 text,
  bundle_sha256 text,
  canonical_prompt_sha256 text,
  addenda_sha256 text[] not null default '{}',
  rule_version text not null,

  universe_decision universe.decision not null,
  verification prod.verification_status not null default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),

  constraint eod_predictions_one_per_session unique (security_id, s0_session_date, thesis_key),
  constraint eod_predictions_market check (market_code in ('JP', 'US')),
  constraint eod_predictions_currency_shape check (signal_price_currency ~ '^[A-Z]{3}$'),
  constraint eod_predictions_prices_are_positive check (
    signal_reference_price > 0 and signal_price_jpy > 0
  ),
  --: Read after the session end plus the feed's publication delay: before
  --: that, a daily bar can still hold a mid-session price (D-262).
  constraint eod_predictions_close_is_confirmed check (
    close_publication_delay_seconds >= 0
    and close_fetched_at >= s0_session_closed_at + make_interval(secs => close_publication_delay_seconds)
  ),
  --: Predicted after the close was confirmed, from information up to at least
  --: that close.
  constraint eod_predictions_time_order check (
    s0_session_closed_at <= data_cutoff
    and data_cutoff <= decision_completed_at
    and close_fetched_at <= decision_completed_at
  ),
  --: The 3,000 yen filter, on the S0 close.
  constraint eod_predictions_under_limit check (signal_price_jpy <= 3000),
  --: Yen is not converted. Anything else is, at a rate observed no later than
  --: the close (CLAUDE.md 1-7, EOD: fx_observed_at <= price_cutoff_at).
  constraint eod_predictions_conversion check (
    (
      signal_price_currency = 'JPY'
      and fx_rate is null and fx_observed_at is null
      and signal_price_jpy = signal_reference_price
    ) or (
      signal_price_currency <> 'JPY'
      and fx_rate > 0 and fx_observed_at is not null
      and fx_observed_at <= s0_session_closed_at
      and abs(signal_price_jpy - signal_reference_price * fx_rate) < 0.000001
    )
  ),
  constraint eod_predictions_target_is_twenty_percent check (
    abs(target_price - signal_reference_price * 1.20) < 0.000001
  ),
  constraint eod_predictions_failure_below_close check (
    initial_failure_line is null
    or (initial_failure_line > 0 and initial_failure_line < signal_reference_price)
  ),
  --: S0 ended at its close, so evaluation starts the next session.
  constraint eod_predictions_evaluation_starts_at_s1 check (evaluation_start_session = 1),
  constraint eod_predictions_checkpoints check (
    checkpoint_sessions = '{1,3,5,10,20}'::smallint[]
  ),
  constraint eod_predictions_horizon_is_twenty check (horizon_sessions = 20),
  constraint eod_predictions_rule_version check (rule_version like 'eod-prediction-%'),
  --: A deterministic stand-in exercises the pipeline; it does not produce claims.
  constraint eod_predictions_not_from_a_mock check (provider_kind <> 'DETERMINISTIC_MOCK'),
  --: UNRESOLVED is not a synonym for included (CLAUDE.md 1-10b).
  constraint eod_predictions_universe_included check (universe_decision = 'INCLUDED')
);

comment on table prod.eod_predictions is
  'Append-only. One after-close prediction: S0''s confirmed close as the reference (signal_reference_price), the 3,000 yen filter on it, target = close x 1.20, evaluation from S1 with checkpoints at T+1/3/5/10/20. Rules: docs/specs/eod-prediction.md. Corrections are new rows, not edits.';
comment on column prod.eod_predictions.signal_reference_price is
  'S0''s confirmed close, as traded (raw). Before the decision by construction, which is correct here: what is required is that it was read after the session end plus the feed''s delay.';
comment on column prod.eod_predictions.signal_price_jpy is
  'The yen value of the close, for the 3,000 yen filter only. A US security''s outcome stays in USD (CLAUDE.md 1-9).';
comment on column prod.eod_predictions.checkpoint_sessions is
  'Sessions after S0 at which the outcome is checked; the last is the deadline. Whether +20% is judged on the intraday high or the close is undecided (D-268) and belongs to the outcome, not here.';

create index if not exists eod_predictions_session_idx
  on prod.eod_predictions (s0_session_date, market_code);

drop trigger if exists prod_eod_predictions_append_only on prod.eod_predictions;
create trigger prod_eod_predictions_append_only
  before update or delete on prod.eod_predictions
  for each row execute function prod.forbid_mutation();

revoke all on table prod.eod_predictions from public;
grant select, insert on prod.eod_predictions to surge_worker_prod;
revoke update, delete on prod.eod_predictions from surge_worker_prod;
grant select on prod.eod_predictions to surge_worker_research, surge_readonly;
