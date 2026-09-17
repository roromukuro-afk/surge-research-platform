-- Phase 2.1: the market schema, and the licence terms as first-class data.
--
-- Two of the four providers we are about to ingest from restrict what may be
-- done with the data far more than they restrict what may be fetched. J-Quants
-- limits use to one natural person's private, non-commercial, non-academic
-- research and obliges deletion on cancellation OR downgrade. EODHD's personal
-- plan permits a non-professional user to store and analyse, and prohibits
-- redistributing or displaying.
--
-- Those are facts about the data, not notes in a runbook, so they live in the
-- database next to it. Anything that would expose the data to a second pair of
-- eyes has to ask this table first.
--
-- What we could NOT establish from the official terms is recorded as
-- NOT_SPECIFIED, never as a convenient false. EODHD's terms contain no
-- delete-on-termination clause; the absence of a clause is not a permission.

create schema if not exists market;
comment on schema market is
  'Market data: prices, corporate actions, FX, identifier mappings, and the licence terms that govern them.';

-- ---------------------------------------------------------------- vocabulary
do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'license_mode' and n.nspname = 'market') then
    create type market.license_mode as enum (
      'PRIVATE_PERSONAL_RESEARCH_ONLY',  -- one natural person, private, non-commercial
      'INTERNAL_COMMERCIAL',             -- an organisation may use it internally
      'PUBLIC_REDISTRIBUTABLE',          -- may be shown and redistributed, usually with conditions
      'PUBLIC_DOMAIN'                    -- dedicated to the public domain
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'license_permission' and n.nspname = 'market') then
    create type market.license_permission as enum (
      'ALLOWED',
      'PROHIBITED',
      'NOT_SPECIFIED',  -- the official terms are silent. Silence is not permission.
      'UNKNOWN'         -- we have not read the governing document yet
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'license_obligation' and n.nspname = 'market') then
    create type market.license_obligation as enum (
      'REQUIRED',
      'NOT_REQUIRED',
      'NOT_SPECIFIED',
      'UNKNOWN'
    );
  end if;
end;
$$;

-- ----------------------------------------------------------------- the table
create table if not exists market.provider_license_policies (
  provider_id text not null,
  dataset_key text not null,
  policy_version text not null,

  license_mode market.license_mode not null,

  public_display_allowed market.license_permission not null,
  third_party_access_allowed market.license_permission not null,
  commercial_use_allowed market.license_permission not null,
  academic_use_allowed market.license_permission not null,
  raw_redistribution_allowed market.license_permission not null,
  derived_output_sharing_allowed market.license_permission not null default 'UNKNOWN',

  delete_on_cancel market.license_obligation not null,
  delete_on_downgrade market.license_obligation not null,
  attribution_required market.license_obligation not null default 'UNKNOWN',
  modification_disclosure_required market.license_obligation not null default 'UNKNOWN',

  entitlement_plan text,
  terms_url text not null,
  terms_checked_at timestamptz not null,
  effective_from timestamptz not null default clock_timestamp(),
  effective_to timestamptz,
  notes text,

  primary key (provider_id, dataset_key, policy_version),
  constraint license_policy_period_ck check (effective_to is null or effective_to > effective_from)
);

comment on table market.provider_license_policies is
  'What each provider permits for each dataset, read from that provider''s own terms. Append-only and versioned: a policy is never edited in place, a new policy_version supersedes it, and stored objects record which version governed them.';
comment on column market.provider_license_policies.policy_version is
  'Stable identifier of this reading of the terms. Every raw object records the version that governed it, so a later change of terms can be traced to the objects it affects.';
comment on column market.provider_license_policies.terms_checked_at is
  'When a human or this project last read the governing document at terms_url. Terms change without notice; a stale check is itself a finding.';
comment on column market.provider_license_policies.delete_on_cancel is
  'Whether the terms oblige deletion of stored data when the subscription ends. NOT_SPECIFIED means the terms say nothing - it does not mean no obligation.';
comment on column market.provider_license_policies.entitlement_plan is
  'The plan this reading applies to. The same provider can grant different rights on different plans (EODHD personal vs commercial; Massive individual vs business).';

create unique index if not exists provider_license_policies_current_uq
  on market.provider_license_policies (provider_id, dataset_key)
  where effective_to is null;

-- Policies are a record of what the terms said. Editing one in place would
-- destroy the evidence that an object was stored under different rules.
create or replace function market.forbid_license_policy_rewrite()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'licence policies are append-only; supersede % / % / % instead of deleting it',
      old.provider_id, old.dataset_key, old.policy_version
      using errcode = 'read_only_sql_transaction';
  end if;

  -- Superseding a reading of the terms is the one legitimate update: set
  -- effective_to and leave every other column exactly as it was recorded.
  if to_jsonb(new) - 'effective_to' is distinct from to_jsonb(old) - 'effective_to' then
    raise exception 'a licence policy is immutable apart from effective_to (% / % / %)',
      old.provider_id, old.dataset_key, old.policy_version
      using errcode = 'read_only_sql_transaction';
  end if;

  if old.effective_to is not null then
    raise exception 'licence policy % / % / % is already superseded and is immutable',
      old.provider_id, old.dataset_key, old.policy_version
      using errcode = 'read_only_sql_transaction';
  end if;

  if new.effective_to is null then
    raise exception 'the only permitted update to a licence policy is setting effective_to'
      using errcode = 'read_only_sql_transaction';
  end if;

  return new;
end;
$$;

drop trigger if exists forbid_license_policy_rewrite on market.provider_license_policies;
create trigger forbid_license_policy_rewrite
  before update or delete on market.provider_license_policies
  for each row execute function market.forbid_license_policy_rewrite();

-- --------------------------------------------------------------- the readers
create or replace function market.current_license_policy(p_provider_id text, p_dataset_key text)
returns market.provider_license_policies
language sql
stable
set search_path = ''
as $$
  select *
  from market.provider_license_policies
  where provider_id = p_provider_id
    and dataset_key = p_dataset_key
    and effective_to is null;
$$;

comment on function market.current_license_policy(text, text) is
  'The reading of the terms in force now for this dataset. Returns no row when the dataset has no policy - which callers must treat as prohibited, not as unrestricted.';

create or replace function market.assert_license_allows(
  p_provider_id text,
  p_dataset_key text,
  p_action text
)
returns void
language plpgsql
stable
set search_path = ''
as $$
declare
  v_policy market.provider_license_policies;
  v_permission market.license_permission;
begin
  select * into v_policy
  from market.provider_license_policies
  where provider_id = p_provider_id and dataset_key = p_dataset_key and effective_to is null;

  if not found then
    raise exception 'no licence policy for % / %; the data cannot be used for % until its terms are recorded',
      p_provider_id, p_dataset_key, p_action
      using errcode = 'insufficient_privilege';
  end if;

  v_permission := case upper(p_action)
    when 'PUBLIC_DISPLAY' then v_policy.public_display_allowed
    when 'THIRD_PARTY_ACCESS' then v_policy.third_party_access_allowed
    when 'COMMERCIAL_USE' then v_policy.commercial_use_allowed
    when 'ACADEMIC_USE' then v_policy.academic_use_allowed
    when 'RAW_REDISTRIBUTION' then v_policy.raw_redistribution_allowed
    when 'DERIVED_OUTPUT_SHARING' then v_policy.derived_output_sharing_allowed
    else null
  end;

  if v_permission is null then
    raise exception 'unknown licence action %', p_action using errcode = 'invalid_parameter_value';
  end if;

  if v_permission <> 'ALLOWED' then
    raise exception '% / % does not permit %: the terms say % (%)',
      p_provider_id, p_dataset_key, p_action, v_permission, v_policy.terms_url
      using errcode = 'insufficient_privilege';
  end if;
end;
$$;

comment on function market.assert_license_allows(text, text, text) is
  'Raises unless the provider''s own terms permit this action on this dataset. PROHIBITED, NOT_SPECIFIED, UNKNOWN and a missing policy all raise: only an explicit ALLOWED passes.';

create or replace view market.license_summary as
  select provider_id,
         dataset_key,
         policy_version,
         license_mode,
         entitlement_plan,
         public_display_allowed,
         third_party_access_allowed,
         commercial_use_allowed,
         academic_use_allowed,
         raw_redistribution_allowed,
         delete_on_cancel,
         delete_on_downgrade,
         terms_checked_at,
         terms_url
  from market.provider_license_policies
  where effective_to is null
  order by provider_id, dataset_key;

comment on view market.license_summary is
  'The licence terms in force, one row per dataset. This is what a licence review reads.';

-- --------------------------------------------------------------- privileges
grant usage on schema market to surge_worker_prod, surge_worker_research, surge_readonly;

-- New tables in this schema are read-only to runtime by default, exactly as in
-- ref / pipeline / universe. A migration that adds a table the worker writes
-- must grant that explicitly.
alter default privileges in schema market
  grant select on tables to surge_worker_prod, surge_worker_research, surge_readonly;
alter default privileges in schema market
  revoke insert, update, delete on tables from surge_worker_prod, surge_worker_research;

revoke all on table market.provider_license_policies from public;
grant select on table market.provider_license_policies
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant select on market.license_summary
  to surge_worker_prod, surge_worker_research, surge_readonly;

grant execute on function market.current_license_policy(text, text)
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant execute on function market.assert_license_allows(text, text, text)
  to surge_worker_prod, surge_worker_research, surge_readonly;

revoke all on function market.forbid_license_policy_rewrite() from public;

-- ------------------------------------------------------------------- the terms
-- Read 2026-09-16. Every value below is what the provider's own document says;
-- where it says nothing, the value is NOT_SPECIFIED.
insert into market.provider_license_policies (
  provider_id, dataset_key, policy_version, license_mode,
  public_display_allowed, third_party_access_allowed, commercial_use_allowed,
  academic_use_allowed, raw_redistribution_allowed, derived_output_sharing_allowed,
  delete_on_cancel, delete_on_downgrade, attribution_required, modification_disclosure_required,
  entitlement_plan, terms_url, terms_checked_at, notes
) values
  -- J-Quants: private use by the registered individual only. Corporate use is
  -- prohibited even internally and non-commercially; academic use is prohibited
  -- outside a student's own undergraduate thesis. Storing in our own cloud is
  -- permitted while only the account holder can see it.
  ('jquants', 'JQ_EQ_BARS_DAILY', 'jquants-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'ALLOWED',
   'REQUIRED', 'REQUIRED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'Standard', 'https://jpx-jquants.com/termsofservice', timestamptz '2026-09-16 00:00:00+00',
   'Terms art.8: private use by the registered user only; making the data (including processed forms) available to third parties, or commercial or academic use, is outside private use. Help Center: storage in one''s own cloud is permitted if only the account holder can view it, with access control and encryption as the user''s responsibility; on cancellation or downgrade the stored data, its copies and any derivative from which the original can be reconstructed must be deleted. Sharing analysis output is permitted but not continuously or repeatedly.'),
  ('jquants', 'JQ_EQ_MASTER', 'jquants-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'ALLOWED',
   'REQUIRED', 'REQUIRED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'Standard', 'https://jpx-jquants.com/termsofservice', timestamptz '2026-09-16 00:00:00+00',
   'Same terms as JQ_EQ_BARS_DAILY.'),

  -- EODHD personal plans: a Non-Professional User may store, manipulate and
  -- analyse for private non-commercial purposes, and may not share, sell,
  -- retransmit, redistribute, display or grant access. The terms contain NO
  -- delete-on-termination clause - which is why both obligations are
  -- NOT_SPECIFIED rather than NOT_REQUIRED.
  ('eodhd', 'EODHD_US_EOD_BULK', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00',
   'Non-Professional Users may store, manipulate and analyze for private non-commercial purposes; sharing account access, and selling, reselling, retransmitting, redistributing, displaying or granting access to the Information are prohibited. Every published plan is labelled Personal use; commercial use is separately licensed. No delete-on-termination clause was found in the terms - recorded as NOT_SPECIFIED, not as NOT_REQUIRED.'),
  ('eodhd', 'EODHD_SPLITS', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00', 'Same terms as EODHD_US_EOD_BULK.'),
  ('eodhd', 'EODHD_DIVIDENDS', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00', 'Same terms as EODHD_US_EOD_BULK.'),
  ('eodhd', 'EODHD_SYMBOL_LIST', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00', 'Same terms as EODHD_US_EOD_BULK.'),
  ('eodhd', 'EODHD_SYMBOL_CHANGES', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00', 'Same terms as EODHD_US_EOD_BULK.'),
  ('eodhd', 'EODHD_FX_EOD', 'eodhd-2026-09-16', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'NOT_SPECIFIED', 'PROHIBITED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED', 'NOT_SPECIFIED',
   'EOD Historical Data - All World', 'https://eodhd.com/financial-apis/terms-conditions',
   timestamptz '2026-09-16 00:00:00+00',
   'Same terms as EODHD_US_EOD_BULK. EODHD additionally states that Forex is not exchange sourced and that its prices are indicative and not appropriate for trading purposes.'),

  -- ECB: free use subject to accurate reproduction, citing the ECB, and stating
  -- explicitly when the information has been modified - which deriving USD/JPY
  -- from two euro legs is.
  ('ecb', 'ECB_EXR_DAILY', 'ecb-2026-09-16', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED',
   'NOT_REQUIRED', 'NOT_REQUIRED', 'REQUIRED', 'REQUIRED',
   null, 'https://www.ecb.europa.eu/services/disclaimer/html/index.en.html',
   timestamptz '2026-09-16 00:00:00+00',
   'Free use subject to: the information must be reproduced accurately and the ECB cited as the source; where it is incorporated in documents that are sold, buyers must be told it is available free of charge; and if the user modifies it (the disclaimer gives calculation of growth rates as an example) that must be stated explicitly. Deriving USD/JPY from the JPY/EUR and USD/EUR legs is such a modification. The ECB also states the reference rates are for information purposes only and discourages their use for transaction purposes.'),

  -- OpenFIGI: FIGI identifiers are dedicated to the public domain, and Bloomberg
  -- undertakes not to repudiate that grant.
  ('openfigi', 'OPENFIGI_MAPPING', 'openfigi-2026-09-16', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED',
   'NOT_REQUIRED', 'NOT_REQUIRED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   null, 'https://www.openfigi.com/docs/terms-of-service', timestamptz '2026-09-16 00:00:00+00',
   'FIGI Identifiers are dedicated to the public domain for any purpose, commercial or non-commercial, including redistribution; the terms state the grant in paragraph 1 will not be terminated, modified or repudiated. Trademark use is separately limited. The API itself is rate limited and the service carries a liability cap.')
on conflict (provider_id, dataset_key, policy_version) do nothing;
