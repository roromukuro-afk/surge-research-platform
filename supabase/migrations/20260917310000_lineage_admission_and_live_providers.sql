-- Live activation, part 2: what a training set may admit, and two real providers.
--
-- Three things, and they are here together because they are the same question
-- asked of three layers: what has actually been recorded, and what may be done
-- on the strength of it.
--
-- 1. A production training dataset may not admit a label whose input side was
--    never written down. `labels.observation_contexts` exists; nothing required
--    it. A dataset built from decisions and outcomes alone teaches a model from
--    inputs nobody can reconstruct, and the reconstruction attempted later will
--    quietly include everything that arrived after the decision.
--
-- 2. `alpaca_historical_sip` is registered and left DISABLED. Its adapter is
--    written and tested; whether Alpaca's terms permit keeping market data in a
--    private research database is NOT_SPECIFIED, and silence is not permission.
--    So the row exists, the licence records exactly what the terms do and do not
--    say, and the role binding is off.
--
-- 3. `groq_hosted` is registered and left disabled, for the same reason at a
--    different layer: the adapter exists and has never been given a credential.

set search_path = '';

-- ---------------------------------------------------------------------------
-- 1. Teacher lineage as a condition of admission
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'dataset_purpose' and n.nspname = 'labels') then
    create type labels.dataset_purpose as enum (
      'PRODUCTION_TRAINING',  -- a model will be trained on this; lineage required
      'RESEARCH_ONLY'         -- error analysis and exploration; lineage not required
    );
  end if;
end;
$$;

alter table labels.datasets
  add column if not exists purpose labels.dataset_purpose not null default 'PRODUCTION_TRAINING';

comment on column labels.datasets.purpose is
  'PRODUCTION_TRAINING is the default deliberately. A dataset that does not say what it is for is treated as one a model will be trained on, so it must carry the input side of every example it admits; defaulting the other way would let an unlineaged set become a training set simply because nobody said otherwise.';

alter table labels.observation_contexts
  add column if not exists no_feature_reason text;

comment on column labels.observation_contexts.no_feature_reason is
  'Why there is no feature_version, when there legitimately is not. Stated rather than inferred from the null, because "no features" and "nobody recorded which features" are different examples and only one of them is usable.';

--: Everything a context must carry before the example it describes may be
--: trained on. Returns the gaps, so the refusal can say which ones.
create or replace function labels.lineage_gaps(
  p_objective_id uuid,
  p_observation_kind labels.observation_kind
)
returns text[]
language plpgsql
stable
set search_path = ''
as $$
declare
  ctx labels.observation_contexts%rowtype;
  gaps text[] := '{}';
begin
  select * into ctx from labels.observation_contexts where objective_id = p_objective_id;
  if not found then
    return array['no observation context: the inputs this decision was made from were never recorded'];
  end if;

  if ctx.coverage_snapshot is null or ctx.coverage_snapshot = '{}'::jsonb then
    gaps := array_append(gaps, 'no coverage_snapshot');
  end if;
  if ctx.feature_version is null and ctx.no_feature_reason is null then
    gaps := array_append(gaps, 'no feature_version and no stated reason for its absence');
  end if;
  if ctx.feature_version is not null
     and ctx.feature_snapshot is null and ctx.feature_snapshot_ref is null then
    gaps := array_append(gaps, 'feature_version names a version but no snapshot or reference');
  end if;
  if ctx.production_run_id is null then
    gaps := array_append(gaps, 'no production_run_id');
  end if;
  if ctx.universe_run_id is null then
    gaps := array_append(gaps, 'no universe_run_id');
  end if;
  if ctx.market_data_run_id is null then
    gaps := array_append(gaps, 'no market_data_run_id');
  end if;

  if p_observation_kind = 'PREDICTED' then
    if ctx.episode_id is null then
      gaps := array_append(gaps, 'PREDICTED with no episode_id');
    end if;
    if ctx.entry_attempt_id is null then
      gaps := array_append(gaps, 'PREDICTED with no entry_attempt_id');
    end if;
    if ctx.stage3_output_id is null and ctx.input_bundle_sha256 is null then
      gaps := array_append(gaps, 'PREDICTED with no decision reference (stage 3 output or bundle hash)');
    end if;
  elsif p_observation_kind = 'SETUP_NOT_ENTERED' then
    if ctx.setup_id is null then
      gaps := array_append(gaps, 'SETUP_NOT_ENTERED with no setup_id');
    end if;
  end if;
  -- ELIGIBLE_ONLY needs the universe, feature and coverage lineage above and
  -- nothing more. Demanding an episode would reject the entire population of
  -- securities nobody surfaced, which is the part that teaches about misses.

  return gaps;
end;
$$;

comment on function labels.lineage_gaps(uuid, labels.observation_kind) is
  'What is missing before this example may be trained on. Empty means complete. The requirements differ by observation kind because the kinds are different objects.';

create or replace function labels.assert_admitted_member_has_lineage()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_purpose labels.dataset_purpose;
  v_kind labels.observation_kind;
  v_objective uuid;
  v_gaps text[];
begin
  if not new.admitted then
    -- A rejected member is the record of something being left out, and the
    -- reason it was left out may well be that its lineage is missing.
    return new;
  end if;

  select purpose into v_purpose from labels.datasets where dataset_id = new.dataset_id;
  if v_purpose is distinct from 'PRODUCTION_TRAINING' then
    return new;
  end if;

  select i.objective_id, o.observation_kind
    into v_objective, v_kind
    from labels.interpretive_labels i
    join labels.objective_labels o on o.objective_id = i.objective_id
   where i.label_id = new.label_id;

  if v_objective is null then
    raise exception
      'label % cannot be admitted: it has no objective observation behind it',
      new.label_id;
  end if;

  v_gaps := labels.lineage_gaps(v_objective, v_kind);
  if coalesce(array_length(v_gaps, 1), 0) > 0 then
    -- coalesce because array_length of an empty array is NULL, and NULL > 0 is
    -- NULL, which is not true - the same three-valued trap that let a watch
    -- move illegally and a refusal carry no reasons.
    raise exception
      'label % may not be admitted to a PRODUCTION_TRAINING dataset: %. Teacher data is input snapshot + decision + outcome; the label itself may be stored, and it may not be trained on',
      new.label_id, array_to_string(v_gaps, '; ');
  end if;

  return new;
end;
$$;

drop trigger if exists labels_dataset_members_need_lineage on labels.dataset_members;
create trigger labels_dataset_members_need_lineage
  before insert or update on labels.dataset_members
  for each row execute function labels.assert_admitted_member_has_lineage();

create or replace view ui.teacher_lineage_admission as
  select d.name                      as dataset,
         d.purpose::text             as purpose,
         count(*) filter (where m.admitted)     as admitted,
         count(*) filter (where not m.admitted) as rejected
    from labels.datasets d
    left join labels.dataset_members m on m.dataset_id = d.dataset_id
   group by d.name, d.purpose;

comment on view ui.teacher_lineage_admission is
  'How much of each dataset survived the lineage requirement. A production set with a large rejected count is not a failure; it is the input side of the teacher data being visibly incomplete.';

grant select on ui.teacher_lineage_admission to surge_web, surge_readonly;

-- ---------------------------------------------------------------------------
-- 2. The persistence permission, which had no column
-- ---------------------------------------------------------------------------

alter table market.provider_license_policies
  add column if not exists private_persistence_allowed market.license_permission
    not null default 'UNKNOWN';

comment on column market.provider_license_policies.private_persistence_allowed is
  'Whether the terms permit keeping this data in a private, non-public research database. The permission every stored row depends on, and until now the one permission with no column: display, redistribution and commercial use were each recorded while "may we keep it at all" was not. NOT_SPECIFIED is the common answer and it is not permission.';

insert into market.providers
  (provider_id, display_name, cost_class, monthly_cost_jpy, monthly_cost_note,
   requires_credential, credential_env_var, official_url, notes)
values
  ('alpaca_historical_sip', 'Alpaca historical bars, consolidated tape (delayed)', 'FREE', 0,
   'The Basic market data plan is free. SIP is readable on the historical endpoints without a subscription provided the query end is at least 15 minutes old.',
   true, 'APCA_API_KEY_ID',
   'https://docs.alpaca.markets/us/docs/market-data-faq',
   'Two credentials, APCA_API_KEY_ID and APCA_API_SECRET_KEY, neither of which is ever stored here. Delayed consolidated history only: the latest endpoints on Basic return IEX alone, which is one exchange and not the session. This provider answers D-103-EOD and cannot answer D-103-LIVE.')
on conflict (provider_id) do update set
  cost_class = excluded.cost_class,
  monthly_cost_jpy = excluded.monthly_cost_jpy,
  monthly_cost_note = excluded.monthly_cost_note,
  notes = excluded.notes;

insert into market.provider_license_policies
  (provider_id, dataset_key, policy_version, license_mode,
   public_display_allowed, third_party_access_allowed, commercial_use_allowed,
   academic_use_allowed, raw_redistribution_allowed, derived_output_sharing_allowed,
   private_persistence_allowed,
   delete_on_cancel, delete_on_downgrade, attribution_required,
   modification_disclosure_required, entitlement_plan, terms_url, terms_checked_at, notes)
values
  ('alpaca_historical_sip', 'ALPACA_US_BARS_SIP', 'alpaca-2026-09-17',
   'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED',
   'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'Basic (no cost)',
   'https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf',
   now(),
   'Deciding clause: "Content is provided exclusively for personal and noncommercial access and use. No part of the Service or Content may be copied, reproduced, republished, uploaded, posted, publicly displayed, encoded, translated, transmitted or distributed in any way (including mirroring) to any other computer, server, web site or other medium for publication or distribution or for any commercial enterprise, without Alpaca''s express prior written consent." Market data is named as Content. The grant of personal, noncommercial use is explicit and the prohibition attaches to publication, distribution and commercial enterprise; a private research database is named by neither, so private_persistence_allowed is NOT_SPECIFIED. Alpaca''s support page states plainly: "Unfortunately, you cannot redistribute Alpaca API data." The Terms bind a user to the NASDAQ and NYSE subscriber agreements on selecting the Pro plan; whether Basic delayed SIP carries the exchanges'' own warehousing terms is not addressed.')
on conflict (provider_id, dataset_key, policy_version) do nothing;

-- Registered and OFF. The adapter is written, tested and credential-free; the
-- binding stays disabled until the persistence question above has an answer,
-- because enabling it is what would start accumulating history.
insert into market.provider_role_bindings
  (binding_version, role, provider_id, dataset_key, enabled, priority, notes)
values
  ('bindings-1.0.0', 'EOD_CURRENT_US', 'alpaca_historical_sip', 'ALPACA_US_BARS_SIP', false, 2,
   'Free, consolidated, delayed 15 minutes. Disabled until Alpaca answers whether market data may be kept in a private research database (private_persistence_allowed = NOT_SPECIFIED).'),
  ('bindings-1.0.0', 'EOD_HISTORY_US', 'alpaca_historical_sip', 'ALPACA_US_BARS_SIP', false, 2,
   'Same provider, same open question.')
on conflict (binding_version, role, provider_id) do nothing;

-- ---------------------------------------------------------------------------
-- 3. The hosted analysis provider
-- ---------------------------------------------------------------------------

insert into analysis.llm_providers
  (provider_id, provider_kind, name, model_id, required_env, monthly_cost_jpy, enabled, notes)
values
  ('groq_hosted', 'HOSTED_LLM', 'Groq (hosted, developer tier)', null,
   array['GROQ_API_KEY', 'GROQ_MODEL'], 0, false,
   'Chosen on how it treats what is sent to it rather than on quality: every request carries Canonical v5.1 in full. Services Agreement: "Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer." Data page: "By default, Groq does not retain customer data for inference requests", with Zero Data Retention selectable in Data Controls and no distinction drawn between the free and paid tiers. The free Gemini tier was rejected on its own terms, which state that submitted content is used to develop Google products and that human reviewers may read it. model_id is null because the model is configuration: naming one here would make an output row''s model_id a fiction the first time the catalogue changed. Enable after ZDR is switched on and one real request has been made.')
on conflict (provider_id) do update set
  required_env = excluded.required_env,
  notes = excluded.notes;
