-- Live activation, part 1: what a teacher example *is*, and what it remembers.
--
-- Three things to settle before the first production teacher row exists, because
-- all three are unfixable afterwards without rewriting history:
--
--   1. Identity. `unique (security_id, as_of_date, label_version)` says "one
--      teacher example per security per day". That is false: a security can
--      have two setups, two theses and two episodes on the same day, and they
--      are different examples with different inputs and different answers.
--      Collapsing them would silently drop one, and which one depends on insert
--      order.
--   2. Lineage. An outcome on its own is not teacher data. Without a record of
--      what the system was looking at when it decided, the input has to be
--      reconstructed later from whatever the tables happen to hold now - which
--      is reconstruction, not observation, and it will quietly include things
--      that arrived after the decision.
--   3. PRICE_SUCCESS_EXOGENOUS in the default admission policy. It means "the
--      price rose, and the thesis does not explain why". Training a predictive
--      model on it teaches the model to claim credit for luck.
--
-- Nothing here has any rows to migrate. That is the point of doing it now.

-- ---------------------------------------------------------------------------
-- 1. Observation identity
-- ---------------------------------------------------------------------------

alter table labels.objective_labels
  add column if not exists observation_key text,
  add column if not exists setup_id uuid references prod.setups (setup_id),
  add column if not exists entry_attempt_id uuid references prod.entry_attempts (attempt_id);

comment on column labels.objective_labels.observation_key is
  'A stable, readable name for this observation, supplied by the labeller. Not parsed by anything - it exists so a person can find the same example again after an identity rule changes.';

--: The old rule said one example per security per day. Two setups on the same
--: security on the same day are two examples.
alter table labels.objective_labels
  drop constraint if exists objective_labels_security_id_as_of_date_label_version_key;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'objective_setup_kind_needs_a_setup') then
    alter table labels.objective_labels add constraint objective_setup_kind_needs_a_setup
      check (observation_kind <> 'SETUP_NOT_ENTERED' or setup_id is not null);
  end if;
  --: An eligible-only observation is a security nobody surfaced. If it has a
  --: setup or an episode, it is not that.
  if not exists (select 1 from pg_constraint where conname = 'objective_eligible_only_has_nothing_attached') then
    alter table labels.objective_labels add constraint objective_eligible_only_has_nothing_attached
      check (
        observation_kind <> 'ELIGIBLE_ONLY'
        or (setup_id is null and episode_id is null and entry_attempt_id is null)
      );
  end if;
end $$;

--: One identity rule per kind, because the three kinds are identified by
--: different things. A partial unique index says exactly that and nothing more.
create unique index if not exists objective_one_per_episode
  on labels.objective_labels (episode_id, label_version)
  where observation_kind = 'PREDICTED';

create unique index if not exists objective_one_per_setup
  on labels.objective_labels (setup_id, label_version)
  where observation_kind = 'SETUP_NOT_ENTERED';

create unique index if not exists objective_one_per_security_day
  on labels.objective_labels (security_id, as_of_date, label_version)
  where observation_kind = 'ELIGIBLE_ONLY';

create unique index if not exists objective_observation_key_unique
  on labels.objective_labels (observation_key, label_version)
  where observation_key is not null;

comment on index labels.objective_one_per_security_day is
  'Only ELIGIBLE_ONLY is identified by security and date, because that is a security nobody surfaced on that day and there is only one of those. A predicted observation is identified by its episode and a setup observation by its setup - same security, same day, different examples.';

-- ---------------------------------------------------------------------------
-- 2. Lineage: what the system was looking at
-- ---------------------------------------------------------------------------

create table if not exists labels.observation_contexts (
  objective_id uuid primary key references labels.objective_labels (objective_id) on delete restrict,

  --: The knowledge boundary. Everything referenced below must have been
  --: available at or before this.
  information_cutoff_at timestamptz not null,

  --: Which runs produced the inputs. Together with the run tables these make
  --: the decision reproducible rather than merely plausible.
  production_run_id uuid references pipeline.runs (run_id),
  universe_run_id uuid references pipeline.runs (run_id),
  market_data_run_id uuid references pipeline.runs (run_id),
  fx_run_id uuid references pipeline.runs (run_id),

  --: What the features were and where they are.
  feature_version text,
  feature_snapshot jsonb,
  feature_snapshot_ref text,

  --: The chain from screening to decision.
  technical_candidate_ref text,
  material_candidate_ref text,
  stage2_assessment_ref text,
  stage3_output_id uuid references analysis.stage3_outputs (output_id),
  input_bundle_sha256 text,

  setup_id uuid references prod.setups (setup_id),
  entry_attempt_id uuid references prod.entry_attempts (attempt_id),
  episode_id uuid references prod.episodes (episode_id),

  --: What the collectors had managed to fetch by the cutoff. A teacher example
  --: whose coverage was 40% is a different example from one whose coverage was
  --: 100%, and without this they look identical.
  pipeline_coverage_ref text,
  collector_coverage_ref text,
  coverage_snapshot jsonb,

  verification_status prod.verification_status not null
    default 'IMPLEMENTED_NOT_LIVE_VERIFIED',
  recorded_at timestamptz not null default clock_timestamp(),

  constraint observation_context_bundle_hash check (
    input_bundle_sha256 is null or input_bundle_sha256 ~ '^[0-9a-f]{64}$'
  )
);

comment on table labels.observation_contexts is
  'The input side of a teacher example. Teacher data is input snapshot + decision + outcome; with only the last two, the input has to be reconstructed later from whatever the tables hold then - which is reconstruction rather than observation, and it quietly includes things that arrived after the decision.';
comment on column labels.observation_contexts.coverage_snapshot is
  'What the collectors had by the cutoff. An example formed on 40% coverage and one formed on 100% are different examples, and without this they are indistinguishable.';

create index if not exists observation_contexts_episode_idx
  on labels.observation_contexts (episode_id);

--: Append-only, and it agrees with the observation it describes.
create or replace function labels.check_observation_context()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  o labels.objective_labels%rowtype;
begin
  select * into o from labels.objective_labels where objective_id = new.objective_id;
  if not found then
    raise exception 'observation context references a missing objective row';
  end if;
  if o.episode_id is distinct from new.episode_id
     or o.setup_id is distinct from new.setup_id
     or o.entry_attempt_id is distinct from new.entry_attempt_id then
    raise exception
      'the context points at a different episode, setup or attempt than the observation it describes';
  end if;
  return new;
end;
$$;

drop trigger if exists labels_observation_context_matches on labels.observation_contexts;
create trigger labels_observation_context_matches
  before insert on labels.observation_contexts
  for each row execute function labels.check_observation_context();

drop trigger if exists labels_observation_context_append_only on labels.observation_contexts;
create trigger labels_observation_context_append_only
  before update or delete on labels.observation_contexts
  for each row execute function labels.forbid_label_mutation();

-- ---------------------------------------------------------------------------
-- 3. Admission policies: one default, and it does not admit luck
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'labels' and t.typname = 'policy_status') then
    create type labels.policy_status as enum ('DEFAULT', 'SUPERSEDED', 'SPECIAL_PURPOSE');
  end if;
end $$;

alter table labels.admission_policies
  add column if not exists status labels.policy_status not null default 'SPECIAL_PURPOSE',
  add column if not exists superseded_by text references labels.admission_policies (policy_version),
  add column if not exists rationale text;

--: Exactly one default, so "the standard policy" is a fact rather than a habit.
create unique index if not exists admission_one_default
  on labels.admission_policies ((status = 'DEFAULT'))
  where status = 'DEFAULT';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'admission_default_excludes_exogenous') then
    alter table labels.admission_policies add constraint admission_default_excludes_exogenous
      check (
        status <> 'DEFAULT'
        or not (admitted_labels && array['PRICE_SUCCESS_EXOGENOUS']::labels.interpretive_label[])
      );
  end if;
end $$;

comment on constraint admission_default_excludes_exogenous on labels.admission_policies is
  'PRICE_SUCCESS_EXOGENOUS means the price rose and the thesis does not explain why. It stays as a label because that distinction is worth recording, and it stays out of the default training target because a model trained on it learns to claim credit for luck. A special-purpose policy may admit it deliberately.';

insert into labels.admission_policies (
  policy_version, description, min_confidence, required_review_status, admitted_labels,
  status, rationale
) values (
  'admission-1.1.0',
  'The standard policy for a predictive model. Human-approved interpretive labels above 0.7 '
  'confidence, excluding PRICE_SUCCESS_EXOGENOUS.',
  0.7,
  array['APPROVED']::labels.human_review_status[],
  array[
    'PREDICTIVE_SUCCESS', 'STATE_CONFIRMED_SUCCESS',
    'FALSE_POSITIVE', 'FAILED_BEFORE_TARGET', 'PRICED_IN_ERROR',
    'REACHABLE_ZONE_ERROR', 'DISTRIBUTION_ERROR', 'FALSE_PULLBACK',
    'ACTIONABLE_FALSE_NEGATIVE'
  ]::labels.interpretive_label[],
  'DEFAULT',
  'PRICE_SUCCESS_EXOGENOUS is deliberately absent: it describes an episode where the price rose '
  'and the entry thesis does not account for it. Keeping it in a predictive target would teach the '
  'model to take credit for outcomes its own reasoning did not anticipate. It remains available to '
  'research and to a SPECIAL_PURPOSE policy that says why it wants it.'
)
on conflict (policy_version) do nothing;

update labels.admission_policies
   set status = 'SUPERSEDED',
       superseded_by = 'admission-1.1.0',
       rationale = 'Admitted PRICE_SUCCESS_EXOGENOUS, which records a rise the thesis does not explain. Kept for the record; not the default.'
 where policy_version = 'admission-1.0.0';

-- ---------------------------------------------------------------------------
-- 4. Promotion thresholds are provisional, and versioned
-- ---------------------------------------------------------------------------

create table if not exists research.promotion_policies (
  policy_version text primary key,
  status labels.policy_status not null default 'SPECIAL_PURPOSE',
  --: True while the numbers are a cautious guess rather than a measured
  --: sufficiency. Everything here starts true.
  provisional boolean not null default true,

  minimum_labels integer not null,
  minimum_classes integer not null,
  minimum_positive_class integer not null,
  minimum_folds integer not null,
  requires_live_verified_labels boolean not null default true,
  requires_calibration boolean not null default true,
  requires_human_approval boolean not null default true,

  basis text not null,
  created_at timestamptz not null default clock_timestamp(),

  constraint promotion_policy_minimums check (
    minimum_labels > 0 and minimum_classes >= 3
    and minimum_positive_class > 0 and minimum_folds >= 1
  )
);

comment on table research.promotion_policies is
  'The numbers a promotion has to clear, as a versioned row rather than a constant in code. They are provisional: nobody has measured what is sufficient here yet, and a threshold hard-coded in a module reads as settled long after anyone remembers it was a guess.';
comment on column research.promotion_policies.provisional is
  'Stays true until sample size, class balance, variance, walk-forward stability and calibration quality have actually been examined. "It reached 200 labels" is not the same statement as "it has enough data".';

create unique index if not exists promotion_policy_one_default
  on research.promotion_policies ((status = 'DEFAULT'))
  where status = 'DEFAULT';

insert into research.promotion_policies (
  policy_version, status, provisional,
  minimum_labels, minimum_classes, minimum_positive_class, minimum_folds, basis
) values (
  'promotion-provisional-1.0.0', 'DEFAULT', true,
  200, 3, 30, 3,
  'Round numbers chosen to be cautious, not derived from anything. There is no teacher data yet, '
  'so there is no basis for a tuned threshold - and a precise-looking minimum would imply there is. '
  'Replace this row with a measured policy once sample size, class balance, variance, walk-forward '
  'stability and calibration quality can be examined; do not raise or lower these because a '
  'particular challenger is close to them.'
)
on conflict (policy_version) do nothing;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on table labels.observation_contexts, research.promotion_policies from public;

grant select on labels.observation_contexts, research.promotion_policies
  to surge_worker_prod, surge_readonly;
grant select, insert on labels.observation_contexts to surge_worker_research;
grant select on research.promotion_policies to surge_worker_research;

create or replace view ui.teacher_lineage_coverage as
  select o.observation_kind::text as observation_kind,
         count(*) as observations,
         count(c.objective_id) as with_context,
         count(*) filter (where c.input_bundle_sha256 is not null) as with_a_bundle_hash,
         count(*) filter (where c.coverage_snapshot is not null) as with_coverage,
         count(*) filter (where c.stage3_output_id is not null) as with_the_analysis
    from labels.objective_labels o
    left join labels.observation_contexts c on c.objective_id = o.objective_id
   group by 1;

comment on view ui.teacher_lineage_coverage is
  'How many teacher examples can actually be reproduced. An example without a context is an outcome with no record of what the system saw, and it is worth knowing how many of those exist before anyone trains on them.';

grant select on ui.teacher_lineage_coverage to surge_web, surge_readonly;
