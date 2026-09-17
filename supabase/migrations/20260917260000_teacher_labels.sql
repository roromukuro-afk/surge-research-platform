-- Phase 10: the teacher dataset.
--
-- The single most damaging thing this schema could allow is a training target
-- that means "the price rose 20% within twenty sessions". That label is easy to
-- compute, looks like ground truth, and teaches a model to predict moves rather
-- than to predict *this system's* decisions being right - which are different
-- questions, and only the second one has a failure line in it.
--
-- So the objective layer is deliberately not a label. It is a set of measured
-- facts about a price path, and the thing a model is trained on is an
-- interpretive label that had to pass a versioned admission policy.
--
-- Three separations the database enforces rather than documents:
--
--   * Objective facts and interpretive judgements live in different tables.
--     One is a measurement; the other is a hypothesis with a confidence and a
--     review status, and a hypothesis that looks like a measurement is how an
--     unreviewed guess ends up as ground truth.
--   * A miss the system could have caught, a miss the pipeline lost, and a move
--     nothing could have predicted are three different values and never merge.
--     Only the first is the prediction model's fault.
--   * The teacher population is every eligible security, not only the ones a
--     formal prediction was made on. Training only on what was predicted teaches
--     a model what was already believed.

create schema if not exists labels;

comment on schema labels is
  'Teacher data. Objective measurements, interpretive judgements, pipeline misses and the versioned policies that decide what a model may be trained on.';

-- ---------------------------------------------------------------------------
-- Add the schema to the project list the privilege guards read
-- ---------------------------------------------------------------------------

create or replace function pipeline.project_schemas()
returns text[]
language sql
immutable
set search_path = ''
as $$
  select array[
    'ref', 'pipeline', 'universe', 'prod', 'research', 'market',
    'screening', 'news', 'material', 'chart', 'analysis', 'ui', 'labels'
  ]::text[];
$$;

-- ---------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'labels' and t.typname = 'interpretive_label') then
    create type labels.interpretive_label as enum (
      -- On an episode that reached the target.
      'PREDICTIVE_SUCCESS',
      'STATE_CONFIRMED_SUCCESS',
      'PRICE_SUCCESS_EXOGENOUS',
      -- On an episode that did not.
      'FALSE_POSITIVE',
      'FAILED_BEFORE_TARGET',
      'PRICED_IN_ERROR',
      'REACHABLE_ZONE_ERROR',
      'DISTRIBUTION_ERROR',
      'FALSE_PULLBACK',
      -- On a security that was never entered.
      'ACTIONABLE_FALSE_NEGATIVE',
      'PIPELINE_MISSED_ACTIONABLE_SIGNAL',
      'OUT_OF_SCOPE_SHOCK',
      'OUT_OF_SCOPE_LATE'
    );
  end if;
end $$;

comment on type labels.interpretive_label is
  'The three false-negative values are deliberately separate. ACTIONABLE_FALSE_NEGATIVE is the model missing something it could see; PIPELINE_MISSED_ACTIONABLE_SIGNAL is the collector losing something the market had; OUT_OF_SCOPE_SHOCK is nobody could have known. Merging them would let a collection bug be recorded as a model failure, and a model failure as bad luck.';

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'labels' and t.typname = 'human_review_status') then
    create type labels.human_review_status as enum (
      'UNREVIEWED', 'APPROVED', 'REJECTED', 'AMENDED'
    );
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'labels' and t.typname = 'observation_kind') then
    create type labels.observation_kind as enum (
      'PREDICTED',      -- a formal prediction was made
      'SETUP_NOT_ENTERED',  -- a setup or watch existed and no entry followed
      'ELIGIBLE_ONLY'   -- in the universe, never surfaced at all
    );
  end if;
end $$;

comment on type labels.observation_kind is
  'The teacher population is all three. Training on PREDICTED alone would teach a model what this system already believed, and would make every miss invisible to it.';

-- ---------------------------------------------------------------------------
-- Objective: measurements, not labels
-- ---------------------------------------------------------------------------

create table if not exists labels.objective_labels (
  objective_id uuid primary key default gen_random_uuid(),
  security_id uuid not null,
  episode_id uuid references prod.episodes (episode_id),
  as_of_date date not null,
  observation_kind labels.observation_kind not null,

  --: The price everything here is measured from. Never decision_price.
  reference_price numeric(18, 6),
  reference_currency text,

  path_resolution prod.path_resolution,
  resolution_granularity text,
  resolved_session_index integer,

  hit_10 boolean,
  hit_20 boolean,
  hit_30 boolean,
  days_to_20 integer,
  mfe numeric(12, 6),
  mae numeric(12, 6),
  failure_line_hit boolean,

  --: Null when the path could not be resolved. Not false: "we could not tell"
  --: is not "it did not happen", and a null is the only honest value.
  hit_20_before_failure boolean,
  failure_before_20 boolean,

  primary_episode_outcome prod.episode_close_reason,
  counterfactual_later_target_hit boolean,

  label_version text not null,
  computed_at timestamptz not null default clock_timestamp(),

  constraint objective_unresolved_is_null check (
    path_resolution is null
    or path_resolution not in ('AMBIGUOUS_PATH', 'UNRESOLVED_MISSING_DATA')
    or (hit_20_before_failure is null and failure_before_20 is null)
  ),
  constraint objective_predicted_has_an_episode check (
    observation_kind <> 'PREDICTED' or episode_id is not null
  ),
  constraint objective_not_predicted_has_no_episode check (
    observation_kind = 'PREDICTED' or episode_id is null
  ),
  constraint objective_currency_shape check (
    reference_currency is null or reference_currency ~ '^[A-Z]{3}$'
  ),
  unique (security_id, as_of_date, label_version)
);

comment on table labels.objective_labels is
  'Measured facts about one security''s price path from one date. Deliberately not a training target: "rose 20% in twenty sessions" is easy to compute, looks like ground truth, and teaches a model to predict moves rather than to predict this system''s decisions being right.';
comment on column labels.objective_labels.hit_20_before_failure is
  'Null when the path was ambiguous or the data was missing. Not false - "we could not tell" and "it did not happen" are different facts, and only one of them is evidence.';

create index if not exists objective_labels_security_idx
  on labels.objective_labels (security_id, as_of_date desc);
create index if not exists objective_labels_kind_idx
  on labels.objective_labels (observation_kind, as_of_date desc);

-- ---------------------------------------------------------------------------
-- Interpretive: hypotheses, with everything needed to disbelieve them
-- ---------------------------------------------------------------------------

create table if not exists labels.interpretive_labels (
  label_id uuid primary key default gen_random_uuid(),
  objective_id uuid not null references labels.objective_labels (objective_id),
  security_id uuid not null,
  episode_id uuid references prod.episodes (episode_id),
  label labels.interpretive_label not null,

  labeler_model_version text not null,
  prompt_version text,
  confidence numeric(6, 4),
  evidence jsonb not null,
  human_review_status labels.human_review_status not null default 'UNREVIEWED',
  reviewed_by text,
  reviewed_at timestamptz,

  --: What the judgement was allowed to see. Material is admissible only when
  --: available_to_model_at <= this.
  information_cutoff_at timestamptz not null,
  input_sha256 text not null,
  supersedes_label_id uuid references labels.interpretive_labels (label_id),
  created_at timestamptz not null default clock_timestamp(),

  constraint interpretive_confidence_range check (
    confidence is null or confidence between 0 and 1
  ),
  constraint interpretive_input_hash check (input_sha256 ~ '^[0-9a-f]{64}$'),
  constraint interpretive_reviewed_has_a_reviewer check (
    human_review_status = 'UNREVIEWED' or (reviewed_by is not null and reviewed_at is not null)
  )
);

comment on table labels.interpretive_labels is
  'Append-only hypotheses. A re-judgement supersedes rather than overwrites, so the record shows what was believed at the time and what changed - which is the only way to tell a corrected label from a convenient one.';
comment on column labels.interpretive_labels.information_cutoff_at is
  'The judgement may only use material with available_to_model_at at or before this. Backfilled information confirms that something existed; it never becomes something the system knew.';

create index if not exists interpretive_labels_objective_idx
  on labels.interpretive_labels (objective_id);
create index if not exists interpretive_labels_label_idx
  on labels.interpretive_labels (label, human_review_status);

--: The consistency constraints of the spec, as a trigger because they span two
--: tables. A label that contradicts the measured path is not a difference of
--: opinion; it is a label about a different episode.
create or replace function labels.check_interpretive_consistency()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  o labels.objective_labels%rowtype;
  success_labels labels.interpretive_label[] := array[
    'PREDICTIVE_SUCCESS', 'STATE_CONFIRMED_SUCCESS', 'PRICE_SUCCESS_EXOGENOUS'
  ]::labels.interpretive_label[];
  miss_labels labels.interpretive_label[] := array[
    'ACTIONABLE_FALSE_NEGATIVE', 'PIPELINE_MISSED_ACTIONABLE_SIGNAL',
    'OUT_OF_SCOPE_SHOCK', 'OUT_OF_SCOPE_LATE'
  ]::labels.interpretive_label[];
begin
  select * into o from labels.objective_labels where objective_id = new.objective_id;
  if not found then
    raise exception 'interpretive label references a missing objective row';
  end if;

  if o.security_id is distinct from new.security_id then
    raise exception 'the label is on a different security than its objective row';
  end if;

  -- An unresolved path supports no judgement about success or failure.
  if o.path_resolution in ('AMBIGUOUS_PATH', 'UNRESOLVED_MISSING_DATA')
     or o.primary_episode_outcome = 'CORPORATE_ACTION_SUSPECTED' then
    raise exception
      'the path for this episode was not resolved (%); no success or failure label may be attached to it',
      coalesce(o.path_resolution::text, o.primary_episode_outcome::text);
  end if;

  if new.label = any(success_labels) then
    if o.path_resolution is distinct from 'TARGET_FIRST' then
      raise exception
        '% requires the target to have been reached first; this path resolved as %',
        new.label, coalesce(o.path_resolution::text, 'nothing');
    end if;
    -- RF-24. The counterfactual reaching the target is not a reason.
    if o.primary_episode_outcome = 'THESIS_INVALIDATED' then
      raise exception
        '% cannot be attached to an episode whose thesis was invalidated; the price reaching the target afterwards is counterfactual and does not make the call right',
        new.label;
    end if;
  end if;

  if new.label = 'PRICED_IN_ERROR' and o.path_resolution = 'TARGET_FIRST' then
    raise exception 'PRICED_IN_ERROR describes an episode that did not reach its target';
  end if;

  if new.label = any(miss_labels) then
    if o.observation_kind = 'PREDICTED' then
      raise exception
        '% is a label about a security that was never entered; this one has an episode',
        new.label;
    end if;
    if o.hit_20 is not true then
      raise exception
        '% is only meaningful where the price did reach +20%%; this one did not', new.label;
    end if;
  elsif o.observation_kind <> 'PREDICTED' then
    raise exception
      '% describes a prediction, and this observation is % - no prediction was made',
      new.label, o.observation_kind;
  end if;

  return new;
end;
$$;

drop trigger if exists labels_interpretive_consistency on labels.interpretive_labels;
create trigger labels_interpretive_consistency
  before insert on labels.interpretive_labels
  for each row execute function labels.check_interpretive_consistency();

create or replace function labels.forbid_label_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'labels.interpretive_labels is append-only; supersede instead of deleting';
  end if;
  -- Review is the one thing that may be added afterwards. Everything the
  -- judgement itself consisted of is fixed.
  if new.label is distinct from old.label
     or new.objective_id is distinct from old.objective_id
     or new.labeler_model_version is distinct from old.labeler_model_version
     or new.confidence is distinct from old.confidence
     or new.evidence is distinct from old.evidence
     or new.information_cutoff_at is distinct from old.information_cutoff_at
     or new.input_sha256 is distinct from old.input_sha256 then
    raise exception
      'only the review columns may change on a label; a re-judgement is a new row that supersedes this one';
  end if;
  return new;
end;
$$;

drop trigger if exists labels_interpretive_append_only on labels.interpretive_labels;
create trigger labels_interpretive_append_only
  before update or delete on labels.interpretive_labels
  for each row execute function labels.forbid_label_mutation();

-- ---------------------------------------------------------------------------
-- Pipeline misses: a different table because they are a different problem
-- ---------------------------------------------------------------------------

create table if not exists labels.pipeline_miss_records (
  record_id bigint generated always as identity primary key,
  objective_id uuid not null references labels.objective_labels (objective_id),
  security_id uuid not null,
  document_id uuid references news.documents (document_id),
  source_key text,

  source_published_at timestamptz,
  available_to_model_at timestamptz not null,
  information_cutoff_at timestamptz not null,
  delay_seconds numeric(14, 3),
  cause text,
  --: The publisher's own claim about when it published, which can be wrong.
  --: Recorded as a claim rather than as a fact.
  source_timestamp_trusted boolean not null default false,
  evidence jsonb,
  created_at timestamptz not null default clock_timestamp(),

  constraint pipeline_miss_is_actually_late check (
    source_published_at is null
    or (source_published_at <= information_cutoff_at
        and available_to_model_at > information_cutoff_at)
  )
);

comment on table labels.pipeline_miss_records is
  'Signals the market had before the cutoff that this system did not. They are collection failures, not prediction failures, and they train the pipeline rather than the model. The check constraint is the definition: published in time, available too late.';
comment on column labels.pipeline_miss_records.source_timestamp_trusted is
  'source_published_at is the publisher''s own claim and can be wrong. Defaulting to untrusted keeps a misdated document from turning a genuine model miss into a pipeline miss.';

create index if not exists pipeline_miss_security_idx
  on labels.pipeline_miss_records (security_id, information_cutoff_at desc);

-- ---------------------------------------------------------------------------
-- Admission: what a model may be trained on
-- ---------------------------------------------------------------------------

create table if not exists labels.admission_policies (
  policy_version text primary key,
  description text not null,
  min_confidence numeric(6, 4),
  required_review_status labels.human_review_status[] not null default '{}',
  admitted_labels labels.interpretive_label[] not null,
  --: Refuses a target that is a single binary. See the table comment.
  forbid_single_binary_target boolean not null default true,
  created_at timestamptz not null default clock_timestamp(),

  constraint admission_needs_more_than_two_classes check (
    not forbid_single_binary_target or array_length(admitted_labels, 1) >= 3
  ),
  constraint admission_confidence_range check (
    min_confidence is null or min_confidence between 0 and 1
  )
);

comment on table labels.admission_policies is
  'A dataset is built through a policy or not at all. The check constraint refuses a policy admitting fewer than three label classes, because a two-class target here is almost always "did it rise 20%" wearing a different name.';

create table if not exists labels.datasets (
  dataset_id uuid primary key default gen_random_uuid(),
  name text not null,
  policy_version text not null references labels.admission_policies (policy_version),
  built_at timestamptz not null default clock_timestamp(),
  knowledge_cutoff timestamptz not null,
  admitted_count integer not null default 0,
  rejected_count integer not null default 0,
  rejection_reasons jsonb not null default '{}',
  git_sha text,
  notes text,
  unique (name, policy_version, knowledge_cutoff)
);

comment on table labels.datasets is
  'The manifest. Rejected labels are counted and their reasons kept: a dataset that only records what it admitted cannot be audited for what it dropped.';

create table if not exists labels.dataset_members (
  dataset_id uuid not null references labels.datasets (dataset_id) on delete cascade,
  label_id uuid not null references labels.interpretive_labels (label_id),
  admitted boolean not null,
  reason text,
  primary key (dataset_id, label_id)
);

comment on table labels.dataset_members is
  'Both the admitted and the rejected, in one table with a flag. Deleting the rejected ones would make every policy look permissive in hindsight.';

-- ---------------------------------------------------------------------------
-- Read contracts
-- ---------------------------------------------------------------------------

create or replace view ui.teacher_label_counts as
  select o.observation_kind::text as observation_kind,
         coalesce(i.label::text, '(unlabelled)') as label,
         coalesce(i.human_review_status::text, '-') as review_status,
         count(*) as rows,
         avg(i.confidence) as mean_confidence
    from labels.objective_labels o
    left join labels.interpretive_labels i on i.objective_id = o.objective_id
   group by 1, 2, 3;

comment on view ui.teacher_label_counts is
  'What the teacher set actually contains, split by how the observation arose. A set that is mostly PREDICTED is a set that will teach a model what this system already believed.';

grant select on ui.teacher_label_counts to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on schema labels from public;
grant usage on schema labels to surge_worker_prod, surge_worker_research, surge_readonly;

revoke all on table
  labels.objective_labels, labels.interpretive_labels, labels.pipeline_miss_records,
  labels.admission_policies, labels.datasets, labels.dataset_members
from public;

--: Teacher data is research output. The production worker reads it and does not
--: write it; the research worker builds it.
grant select on
  labels.objective_labels, labels.interpretive_labels, labels.pipeline_miss_records,
  labels.admission_policies, labels.datasets, labels.dataset_members
to surge_worker_prod, surge_readonly;

grant select, insert on
  labels.objective_labels, labels.interpretive_labels, labels.pipeline_miss_records,
  labels.datasets, labels.dataset_members
to surge_worker_research;

--: The review columns, and nothing else.
grant update (human_review_status, reviewed_by, reviewed_at)
  on labels.interpretive_labels to surge_worker_research;

--: Policies are changed by migration, not by the job that would benefit from
--: a looser one.
grant select on labels.admission_policies to surge_worker_research;

alter default privileges in schema labels grant select on tables to surge_readonly;

-- ---------------------------------------------------------------------------
-- A starting policy
-- ---------------------------------------------------------------------------

insert into labels.admission_policies (
  policy_version, description, min_confidence, required_review_status, admitted_labels
) values (
  'admission-1.0.0',
  'Conservative opening policy: human-approved interpretive labels only, above 0.7 confidence. '
  'Deliberately admits nothing automatically - there is no calibration yet, so a confidence number '
  'is a model''s opinion of itself rather than a measured probability.',
  0.7,
  array['APPROVED']::labels.human_review_status[],
  array[
    'PREDICTIVE_SUCCESS', 'STATE_CONFIRMED_SUCCESS', 'PRICE_SUCCESS_EXOGENOUS',
    'FALSE_POSITIVE', 'FAILED_BEFORE_TARGET', 'PRICED_IN_ERROR',
    'REACHABLE_ZONE_ERROR', 'DISTRIBUTION_ERROR', 'FALSE_PULLBACK',
    'ACTIONABLE_FALSE_NEGATIVE'
  ]::labels.interpretive_label[]
)
on conflict (policy_version) do nothing;

comment on column labels.admission_policies.admitted_labels is
  'PIPELINE_MISSED_ACTIONABLE_SIGNAL, OUT_OF_SCOPE_SHOCK and OUT_OF_SCOPE_LATE are absent from the opening policy on purpose: none of them is a prediction-model failure, and training on them would teach the model to blame itself for a collector outage.';
