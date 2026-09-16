-- Phase 1.1a: an honest, closed vocabulary for identity confidence.
--
-- A US security key is `US:CIK:<cik>:<security_type>:<class token>`. The CIK is
-- a registry identifier for the *issuer*; the type and the class token are read
-- out of the provider's display name. A pure formatting change ("Common Stock"
-- -> "Class A Common Stock") therefore moves the key. Calling that STRONG
-- claims a stability the key does not have, so it becomes REGISTRY_ANCHORED.
--
--   STRONG            - the key is a registry identifier of the thing itself
--                       (SEC CIK for an issuer, EDINET code for an issuer,
--                       JPX local code for a JP security).
--   REGISTRY_ANCHORED - anchored on a registry identifier plus attributes read
--                       from provider text. Stable in practice, not guaranteed.
--   PROVISIONAL       - no registry identifier at all.
--
-- Nothing is promoted automatically: a promotion is a recorded identity
-- migration, which is why ref.securities/ref.issuers also carry the version of
-- the ruleset that assigned their key.

create or replace function ref.identity_confidence_rank(p_confidence text)
returns integer
language sql
immutable
set search_path = ''
as $$
  select case p_confidence
           when 'STRONG' then 3
           when 'REGISTRY_ANCHORED' then 2
           when 'PROVISIONAL' then 1
           else 0
         end;
$$;

create or replace function ref.identity_confidence_label(p_rank integer)
returns text
language sql
immutable
set search_path = ''
as $$
  select case p_rank
           when 3 then 'STRONG'
           when 2 then 'REGISTRY_ANCHORED'
           else 'PROVISIONAL'
         end;
$$;

comment on function ref.identity_confidence_rank(text) is
  'Orders confidence deliberately. Aggregating with min() over the text values happens to work today only because of alphabetical accident; rank makes the pessimistic choice explicit.';

grant execute on function ref.identity_confidence_rank(text), ref.identity_confidence_label(integer)
  to surge_worker_prod, surge_worker_research, surge_readonly;

-- The vocabulary is closed, so a typo fails the load instead of silently
-- creating a fourth tier nothing understands.
alter table ref.issuers drop constraint if exists issuers_identity_confidence_ck;
alter table ref.issuers add constraint issuers_identity_confidence_ck
  check (identity_confidence in ('STRONG', 'REGISTRY_ANCHORED', 'PROVISIONAL'));

alter table ref.securities drop constraint if exists securities_identity_confidence_ck;
alter table ref.securities add constraint securities_identity_confidence_ck
  check (identity_confidence in ('STRONG', 'REGISTRY_ANCHORED', 'PROVISIONAL'));

alter table pipeline.master_snapshot drop constraint if exists master_snapshot_identity_confidence_ck;
alter table pipeline.master_snapshot add constraint master_snapshot_identity_confidence_ck
  check (
    (issuer_identity_confidence is null
      or issuer_identity_confidence in ('STRONG', 'REGISTRY_ANCHORED', 'PROVISIONAL'))
    and (security_identity_confidence is null
      or security_identity_confidence in ('STRONG', 'REGISTRY_ANCHORED', 'PROVISIONAL'))
  );

comment on column ref.securities.identity_confidence is
  'STRONG = registry identifier of the security itself. REGISTRY_ANCHORED = registry issuer id plus attributes derived from provider text (a formatting change can move the key). PROVISIONAL = no registry identifier.';
comment on column ref.issuers.identity_confidence is
  'STRONG = SEC CIK or EDINET code. PROVISIONAL = keyed on the security coordinate; the name is evidence only and never merges two issuers.';
