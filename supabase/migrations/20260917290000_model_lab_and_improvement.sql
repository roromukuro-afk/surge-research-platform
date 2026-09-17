-- Phase 12: the continuous-improvement surfaces.
--
-- Every view here is allowed to return nothing, and right now every one of them
-- does. That is the design constraint, not an accident of timing: a dashboard
-- that fills itself with synthetic numbers while waiting for real ones is worse
-- than an empty dashboard, because the caveat is forgotten long before the
-- numbers are replaced.
--
-- So there is no seeded model, no example champion, no demonstration accuracy.
-- The tables are empty and the screens say so.
--
-- One thing this migration adds beyond views: ``research.episode_attribution``.
-- Route-level performance needs to know which routes fired for an episode, and
-- the alternative was parsing it back out of ``thesis_key`` - which encodes the
-- routes today and is explicitly provisional (D-17a). Recording it is the
-- difference between a fact and an inference from a naming convention.

-- ---------------------------------------------------------------------------
-- What drove each episode
-- ---------------------------------------------------------------------------

create table if not exists research.episode_attribution (
  episode_id uuid primary key references prod.episodes (episode_id) on delete restrict,
  security_id uuid not null,
  as_of_date date not null,

  technical_routes text[] not null default '{}',
  material_routes text[] not null default '{}',
  material_event_types text[] not null default '{}',
  chart_concepts text[] not null default '{}',

  --: Which model produced the Stage 3 verdict this episode came from, so a
  --: change in performance can be attributed to a change in the model rather
  --: than to the market.
  provider_id text,
  provider_kind text,
  model_id text,
  route_version text,
  feature_version text,

  recorded_at timestamptz not null default clock_timestamp()
);

comment on table research.episode_attribution is
  'Which routes, materials and concepts an episode came from. Recorded rather than parsed back out of thesis_key: that key encodes the routes today and is explicitly provisional, and a performance breakdown built on a naming convention silently changes meaning when the convention does.';

create index if not exists episode_attribution_date_idx
  on research.episode_attribution (as_of_date desc);

-- ---------------------------------------------------------------------------
-- The model lab's own record
-- ---------------------------------------------------------------------------

create table if not exists research.model_versions (
  model_version text primary key,
  description text not null,
  trained_at timestamptz,
  dataset_id uuid references labels.datasets (dataset_id),
  git_sha text,
  --: Until a model has been trained on live-verified labels this stays false,
  --: and the promotion gate refuses on it.
  trained_on_live_verified_labels boolean not null default false,
  created_at timestamptz not null default clock_timestamp()
);

comment on table research.model_versions is
  'Models that exist. A row here is not a model that may be used: promotion is a separate record with its own gate, and nothing is promoted by having been trained.';

create table if not exists research.walk_forward_runs (
  run_id uuid primary key default gen_random_uuid(),
  model_version text not null references research.model_versions (model_version),
  dataset_id uuid references labels.datasets (dataset_id),
  fold_count integer not null,
  purge_days integer not null,
  metric_name text not null,
  ran_at timestamptz not null default clock_timestamp(),
  notes text,

  --: The purge gap is the horizon in calendar days. Shorter, and a training
  --: label's answer came from inside its own test window.
  constraint walk_forward_purge_covers_the_horizon check (purge_days >= 30),
  constraint walk_forward_needs_folds check (fold_count >= 1)
);

comment on constraint walk_forward_purge_covers_the_horizon on research.walk_forward_runs is
  'A label dated at the end of training is not settled until about S20. A gap shorter than that trains on answers the test window contains, and the resulting score is not a score.';

create table if not exists research.fold_results (
  run_id uuid not null references research.walk_forward_runs (run_id) on delete cascade,
  fold_index integer not null,
  train_start date not null,
  train_end date not null,
  test_start date not null,
  test_end date not null,
  labels_in_train integer not null,
  labels_in_test integer not null,
  score numeric(12, 6),
  primary key (run_id, fold_index),

  constraint fold_is_time_ordered check (train_end < test_start),
  constraint fold_windows_are_forward check (train_start <= train_end and test_start <= test_end)
);

comment on table research.fold_results is
  'One row per fold. score is nullable because a fold with too few test labels has no meaningful score, and an empty cell is a better answer than a number computed over nine examples.';

create table if not exists research.calibration_bins (
  run_id uuid not null references research.walk_forward_runs (run_id) on delete cascade,
  bin_lower numeric(6, 4) not null,
  bin_upper numeric(6, 4) not null,
  predicted_mean numeric(6, 4),
  observed_rate numeric(6, 4),
  sample_count integer not null,
  primary key (run_id, bin_lower, bin_upper),

  constraint calibration_bin_is_a_range check (bin_lower < bin_upper),
  constraint calibration_bin_within_unit check (bin_lower >= 0 and bin_upper <= 1)
);

comment on table research.calibration_bins is
  'Predicted probability against observed frequency. CLAUDE.md 1-17: until these exist and agree, no probability is shown anywhere - a model reporting 0.73 without a calibration is reporting its opinion of itself.';

create table if not exists research.promotion_attempts (
  attempt_id uuid primary key default gen_random_uuid(),
  challenger_version text not null references research.model_versions (model_version),
  champion_version text references research.model_versions (model_version),
  walk_forward_run_id uuid references research.walk_forward_runs (run_id),
  attempted_at timestamptz not null default clock_timestamp(),
  promoted boolean not null,
  --: Every failing condition, not just the first. A caller who fixes one and
  --: re-runs should see the rest immediately.
  refusal_reasons text[] not null default '{}',
  gate_version text not null,
  evidence jsonb,
  human_approved_by text,

  --: coalesce, because array_length on an empty array is NULL, and a CHECK that
  --: evaluates to NULL passes. The same trap as a CASE branch on NULL.
  constraint promotion_refusal_has_reasons check (
    promoted or coalesce(array_length(refusal_reasons, 1), 0) >= 1
  ),
  constraint promotion_success_has_no_reasons check (
    not promoted or coalesce(array_length(refusal_reasons, 1), 0) = 0
  )
);

comment on table research.promotion_attempts is
  'The audit log for the gate, refusals included. A log of only successful promotions cannot answer the question anybody will actually ask, which is why the last five were refused.';

create index if not exists promotion_attempts_time_idx
  on research.promotion_attempts (attempted_at desc);

-- ---------------------------------------------------------------------------
-- Read contracts. All of them return nothing today.
-- ---------------------------------------------------------------------------

create or replace view ui.teacher_data_summary as
  select o.observation_kind::text as observation_kind,
         count(*) as observations,
         count(*) filter (where o.hit_20) as reached_target,
         count(i.label_id) as labelled,
         count(*) filter (where i.human_review_status = 'APPROVED') as approved
    from labels.objective_labels o
    left join labels.interpretive_labels i on i.objective_id = o.objective_id
   group by 1;

comment on view ui.teacher_data_summary is
  'The teacher population by how the observation arose. A set that is mostly PREDICTED will teach a model what this system already believed.';

create or replace view ui.miss_breakdown as
  select i.label::text as miss_kind,
         case i.label
           when 'ACTIONABLE_FALSE_NEGATIVE' then 'prediction model'
           when 'PIPELINE_MISSED_ACTIONABLE_SIGNAL' then 'collection pipeline'
           when 'OUT_OF_SCOPE_SHOCK' then 'nobody'
           when 'OUT_OF_SCOPE_LATE' then 'timing'
         end as whose_failure,
         count(*) as episodes,
         avg(i.confidence) as mean_confidence,
         count(*) filter (where i.human_review_status = 'APPROVED') as approved
    from labels.interpretive_labels i
   where i.label in (
           'ACTIONABLE_FALSE_NEGATIVE', 'PIPELINE_MISSED_ACTIONABLE_SIGNAL',
           'OUT_OF_SCOPE_SHOCK', 'OUT_OF_SCOPE_LATE')
   group by 1, 2;

comment on view ui.miss_breakdown is
  'The three kinds of miss, separated, with whose failure each one is. Presenting a single "missed" number would let a collector outage read as a model failure and a model failure read as bad luck.';

create or replace view ui.route_performance as
  select unnest(a.technical_routes) as route,
         'TECHNICAL' as route_kind,
         count(*) as episodes,
         count(*) filter (where o.primary_episode_outcome = 'TARGET_HIT') as reached_target,
         count(*) filter (where o.primary_episode_outcome = 'INITIAL_FAILURE_HIT') as hit_failure,
         count(*) filter (where o.primary_episode_outcome in
           ('AMBIGUOUS_PATH', 'UNRESOLVED_MISSING_DATA', 'CORPORATE_ACTION_SUSPECTED')) as unresolved,
         avg(o.counterfactual_mfe) as mean_mfe,
         avg(o.counterfactual_mae) as mean_mae
    from research.episode_attribution a
    join prod.episode_outcomes o on o.episode_id = a.episode_id
   group by 1, 2
  union all
  select unnest(a.material_routes),
         'MATERIAL',
         count(*),
         count(*) filter (where o.primary_episode_outcome = 'TARGET_HIT'),
         count(*) filter (where o.primary_episode_outcome = 'INITIAL_FAILURE_HIT'),
         count(*) filter (where o.primary_episode_outcome in
           ('AMBIGUOUS_PATH', 'UNRESOLVED_MISSING_DATA', 'CORPORATE_ACTION_SUSPECTED')),
         avg(o.counterfactual_mfe),
         avg(o.counterfactual_mae)
    from research.episode_attribution a
    join prod.episode_outcomes o on o.episode_id = a.episode_id
   group by 1, 2;

comment on view ui.route_performance is
  'Per route, with the unresolved episodes counted separately rather than folded into either side. A hit rate that quietly drops the ambiguous ones is a hit rate over the cases that happened to be easy to measure.';

create or replace view ui.material_driver_performance as
  select unnest(a.material_event_types) as driver,
         count(*) as episodes,
         count(*) filter (where o.primary_episode_outcome = 'TARGET_HIT') as reached_target,
         count(*) filter (where o.primary_episode_outcome = 'INITIAL_FAILURE_HIT') as hit_failure,
         count(*) filter (where o.primary_episode_outcome in
           ('AMBIGUOUS_PATH', 'UNRESOLVED_MISSING_DATA', 'CORPORATE_ACTION_SUSPECTED')) as unresolved
    from research.episode_attribution a
    join prod.episode_outcomes o on o.episode_id = a.episode_id
   group by 1;

create or replace view ui.concept_performance as
  select unnest(a.chart_concepts) as concept,
         count(*) as episodes,
         count(*) filter (where o.primary_episode_outcome = 'TARGET_HIT') as reached_target,
         count(*) filter (where o.primary_episode_outcome = 'INITIAL_FAILURE_HIT') as hit_failure,
         avg(o.counterfactual_mfe) as mean_mfe
    from research.episode_attribution a
    join prod.episode_outcomes o on o.episode_id = a.episode_id
   group by 1;

create or replace view ui.llm_judgement_audit as
  select s.as_of_date,
         s.provider_id,
         s.provider_kind::text as provider_kind,
         s.model_id,
         s.state::text as state,
         s.validation_status::text as validation_status,
         count(*) as answers,
         count(*) filter (where s.provider_kind = 'DETERMINISTIC_MOCK') as from_a_stand_in
    from analysis.stage3_outputs s
   group by 1, 2, 3, 4, 5, 6;

comment on view ui.llm_judgement_audit is
  'What the model said, how often, and how often the validator rejected it. from_a_stand_in is shown on every row because a stand-in verdict in a results table is indistinguishable from a real one a year later.';

create or replace view ui.model_version_comparison as
  select m.model_version,
         m.description,
         m.trained_at,
         m.trained_on_live_verified_labels,
         d.name as dataset_name,
         d.admitted_count as labels_admitted,
         w.fold_count,
         w.metric_name,
         (select avg(f.score) from research.fold_results f where f.run_id = w.run_id) as mean_score,
         (select count(*) from research.fold_results f
           where f.run_id = w.run_id and f.score is null) as folds_without_a_score,
         (select count(*) from research.calibration_bins c where c.run_id = w.run_id) as calibration_bins
    from research.model_versions m
    left join labels.datasets d on d.dataset_id = m.dataset_id
    left join research.walk_forward_runs w on w.model_version = m.model_version;

create or replace view ui.walk_forward_results as
  select w.run_id,
         w.model_version,
         w.metric_name,
         w.purge_days,
         f.fold_index,
         f.train_start,
         f.train_end,
         f.test_start,
         f.test_end,
         f.labels_in_train,
         f.labels_in_test,
         f.score
    from research.walk_forward_runs w
    join research.fold_results f on f.run_id = w.run_id;

create or replace view ui.calibration_diagnostics as
  select c.run_id,
         w.model_version,
         c.bin_lower,
         c.bin_upper,
         c.predicted_mean,
         c.observed_rate,
         c.sample_count,
         case
           when c.predicted_mean is null or c.observed_rate is null then null
           else c.observed_rate - c.predicted_mean
         end as gap
    from research.calibration_bins c
    join research.walk_forward_runs w on w.run_id = c.run_id;

comment on view ui.calibration_diagnostics is
  'Predicted against observed. Until this has rows, no probability is displayed anywhere in the application (CLAUDE.md 1-17).';

create or replace view ui.promotion_audit as
  select a.attempt_id,
         a.attempted_at,
         a.challenger_version,
         a.champion_version,
         a.promoted,
         a.refusal_reasons,
         a.gate_version,
         a.human_approved_by,
         (select avg(f.score) from research.fold_results f
           where f.run_id = a.walk_forward_run_id) as mean_score
    from research.promotion_attempts a;

comment on view ui.promotion_audit is
  'Every promotion attempt with the reasons it was refused. A log of successes only cannot answer the question anybody actually asks.';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table
  research.episode_attribution, research.model_versions, research.walk_forward_runs,
  research.fold_results, research.calibration_bins, research.promotion_attempts
from public;

grant select, insert on
  research.episode_attribution, research.model_versions, research.walk_forward_runs,
  research.fold_results, research.calibration_bins, research.promotion_attempts
to surge_worker_research;

grant select on
  research.episode_attribution, research.model_versions, research.walk_forward_runs,
  research.fold_results, research.calibration_bins, research.promotion_attempts
to surge_readonly;

--: The production worker records what drove an episode; everything else in the
--: model lab is research output and it only reads.
grant select, insert on research.episode_attribution to surge_worker_prod;

grant select on
  ui.teacher_data_summary, ui.miss_breakdown, ui.route_performance,
  ui.material_driver_performance, ui.concept_performance, ui.llm_judgement_audit,
  ui.model_version_comparison, ui.walk_forward_results, ui.calibration_diagnostics,
  ui.promotion_audit
to surge_web, surge_readonly;
