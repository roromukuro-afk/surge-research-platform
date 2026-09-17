-- Phase 2.1: the raw object manifest, and the ability to prove a purge.
--
-- J-Quants obliges deletion of stored data on cancellation AND on downgrade.
-- That obligation is only meetable if, at any moment, we can enumerate every
-- object we hold that came from a given provider, dataset and plan entitlement
-- - across object storage, the local cache and Postgres - and then show that
-- each one was deleted. So the manifest is written before the data is useful,
-- not after, and the purge leaves a record of its own.
--
-- The purge is NOT a runtime capability. surge_worker_prod has no DELETE in any
-- schema and does not get one here: purging runs as a separate principal
-- (surge_purge) through SECURITY DEFINER functions, so a compromised or buggy
-- ingestion job cannot erase history.

-- --------------------------------------------------------------- vocabulary
do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'availability_basis' and n.nspname = 'market') then
    -- How we know when the system could first have known a fact. Phase 2 must
    -- not let "fetched today" masquerade as "known on the trading day".
    create type market.availability_basis as enum (
      'OBSERVED_NOW',                  -- we fetched it; available_at is our own receipt time
      'PROVIDER_PUBLISHED_TIMESTAMP',  -- the provider stated when it published (e.g. Last-Modified)
      'DOCUMENTED_SCHEDULE',           -- derived from the provider's published schedule
      'HISTORICAL_REPLAY_ASSUMPTION'   -- an assumption made by a replay, not a fact
    );
  end if;

  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'purge_status' and n.nspname = 'market') then
    create type market.purge_status as enum ('PENDING', 'DELETED', 'NOT_FOUND', 'FAILED', 'SKIPPED');
  end if;
end;
$$;

-- ------------------------------------------------------------- plan ordering
create table if not exists market.provider_plans (
  provider_id text not null,
  plan text not null,
  plan_rank integer not null,
  notes text,
  primary key (provider_id, plan)
);

comment on table market.provider_plans is
  'The ordering of a provider''s plans, so a downgrade can be expressed as "everything that needed a rank above the retained plan must go".';

insert into market.provider_plans (provider_id, plan, plan_rank, notes) values
  ('jquants', 'Free', 0, 'Daily bars are delayed 12 weeks; not usable for production EOD.'),
  ('jquants', 'Light', 1, 'Daily bars back 5 years.'),
  ('jquants', 'Standard', 2, 'Daily bars back 10 years. The plan this project subscribes to.'),
  ('jquants', 'Premium', 3, 'Daily bars back 20 years; dividends.'),
  ('eodhd', 'Free', 0, 'Past year only, 20 API calls/day.'),
  ('eodhd', 'EOD Historical Data - All World', 1, 'The plan this project subscribes to.'),
  ('eodhd', 'EOD+Intraday - All World Extended', 2, null),
  ('eodhd', 'ALL-IN-ONE Package', 3, null),
  ('ecb', 'public', 0, 'No plan; the data is published free of charge.'),
  ('openfigi', 'public', 0, 'No plan; keyless at the lower rate limit.')
on conflict (provider_id, plan) do nothing;

-- --------------------------------------------------------------- the manifest
create table if not exists market.raw_objects (
  object_key text primary key,
  store_id text not null,

  provider_id text not null,
  dataset_key text not null,
  license_policy_version text not null,
  entitlement_plan text not null,
  required_min_plan text not null,

  sha256 text not null,
  bytes bigint not null,
  content_type text not null,

  data_from date,
  data_to date,

  request_url text not null,
  request_params jsonb not null default '{}'::jsonb,
  http_status integer,

  observed_at timestamptz not null,
  available_at timestamptz not null,
  availability_basis market.availability_basis not null,
  source_published_at timestamptz,
  ingested_at timestamptz not null default clock_timestamp(),

  run_id uuid references pipeline.runs (run_id),
  created_at timestamptz not null default clock_timestamp(),

  purged_at timestamptz,
  purge_request_id bigint,

  constraint raw_objects_sha256_ck check (sha256 ~ '^[0-9a-f]{64}$'),
  constraint raw_objects_bytes_ck check (bytes >= 0),
  constraint raw_objects_range_ck check (data_from is null or data_to is null or data_to >= data_from),
  constraint raw_objects_license_fk
    foreign key (provider_id, dataset_key, license_policy_version)
    references market.provider_license_policies (provider_id, dataset_key, policy_version),
  constraint raw_objects_plan_fk
    foreign key (provider_id, entitlement_plan) references market.provider_plans (provider_id, plan),
  constraint raw_objects_min_plan_fk
    foreign key (provider_id, required_min_plan) references market.provider_plans (provider_id, plan)
);

comment on table market.raw_objects is
  'Every raw provider payload we hold: where it is, what it is, which plan entitled us to it and which reading of the terms governed it. This is the list a licence purge works from.';
comment on column market.raw_objects.object_key is
  'Content addressed and write-once: raw/<provider>/<dataset>/<sha256>.<ext>. The same bytes fetched twice land on the same key, and different bytes never collide with an existing one.';
comment on column market.raw_objects.required_min_plan is
  'The lowest plan that entitles us to this object. A downgrade to a plan ranked below this obliges deletion.';
comment on column market.raw_objects.available_at is
  'When the system could first have known this payload. Read availability_basis before trusting it: OBSERVED_NOW on a ten-year-old bar means we learned it today, not then.';
comment on column market.raw_objects.purged_at is
  'Set when a licence purge deleted the bytes. The manifest row stays: the record that we held it, and no longer do, is itself evidence.';

create index if not exists raw_objects_provider_dataset_idx
  on market.raw_objects (provider_id, dataset_key, data_from);
create index if not exists raw_objects_live_idx
  on market.raw_objects (provider_id, dataset_key) where purged_at is null;
create index if not exists raw_objects_run_idx on market.raw_objects (run_id);

-- ------------------------------------------------------------ purge requests
create table if not exists market.purge_requests (
  purge_request_id bigint generated always as identity primary key,
  provider_id text not null,
  dataset_key text,
  retained_plan text,
  reason text not null,
  requested_by text not null,
  requested_at timestamptz not null default clock_timestamp(),
  dry_run boolean not null default true,
  completed_at timestamptz,
  target_count integer,
  deleted_count integer,
  notes text
);

comment on table market.purge_requests is
  'A licence purge: why, what it covered, and whether it finished. A cancellation purges everything for the provider; a downgrade purges only what the retained plan does not entitle us to.';
comment on column market.purge_requests.retained_plan is
  'The plan still held after a downgrade. Null means the subscription ended entirely and every object for the scope must go.';

create table if not exists market.purge_targets (
  purge_request_id bigint not null references market.purge_requests (purge_request_id),
  object_key text not null references market.raw_objects (object_key),
  store_id text not null,
  status market.purge_status not null default 'PENDING',
  detail text,
  resolved_at timestamptz,
  primary key (purge_request_id, object_key)
);

comment on table market.purge_targets is
  'Every object a purge was responsible for, and what happened to it. An unresolved PENDING row is an unmet deletion obligation, not a tidy-up task.';

create index if not exists purge_targets_pending_idx
  on market.purge_targets (purge_request_id) where status = 'PENDING';

alter table market.raw_objects
  drop constraint if exists raw_objects_purge_fk;
alter table market.raw_objects
  add constraint raw_objects_purge_fk
  foreign key (purge_request_id) references market.purge_requests (purge_request_id);

-- ------------------------------------------------------- append-only manifest
create or replace function market.protect_raw_object_manifest()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'raw_objects is append-only; record the purge instead of deleting the manifest row for %',
      old.object_key using errcode = 'read_only_sql_transaction';
  end if;

  if (new.object_key, new.store_id, new.provider_id, new.dataset_key, new.license_policy_version,
      new.entitlement_plan, new.required_min_plan, new.sha256, new.bytes, new.content_type,
      new.data_from, new.data_to, new.request_url, new.request_params, new.http_status,
      new.observed_at, new.available_at, new.availability_basis, new.source_published_at,
      new.ingested_at, new.run_id, new.created_at)
     is distinct from
     (old.object_key, old.store_id, old.provider_id, old.dataset_key, old.license_policy_version,
      old.entitlement_plan, old.required_min_plan, old.sha256, old.bytes, old.content_type,
      old.data_from, old.data_to, old.request_url, old.request_params, old.http_status,
      old.observed_at, old.available_at, old.availability_basis, old.source_published_at,
      old.ingested_at, old.run_id, old.created_at) then
    raise exception 'a raw object manifest row is immutable apart from its purge columns (%)', old.object_key
      using errcode = 'read_only_sql_transaction';
  end if;

  if old.purged_at is not null and new.purged_at is null then
    raise exception 'a purge cannot be un-recorded (%)', old.object_key
      using errcode = 'read_only_sql_transaction';
  end if;

  return new;
end;
$$;

drop trigger if exists protect_raw_object_manifest on market.raw_objects;
create trigger protect_raw_object_manifest
  before update or delete on market.raw_objects
  for each row execute function market.protect_raw_object_manifest();

-- ---------------------------------------------------------- the purge itself
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'surge_purge') then
    create role surge_purge nologin;
  end if;
end;
$$;

-- surge_purge: licence compliance principal. Holds the deletion rights that
-- surge_worker_prod deliberately does not.

grant usage on schema market to surge_purge;
grant select on all tables in schema market to surge_purge;
alter default privileges in schema market grant select on tables to surge_purge;

create or replace function market.plan_rank(p_provider_id text, p_plan text)
returns integer
language sql
stable
set search_path = ''
as $$
  select plan_rank from market.provider_plans where provider_id = p_provider_id and plan = p_plan;
$$;

create or replace function market.open_purge_request(
  p_provider_id text,
  p_dataset_key text,
  p_retained_plan text,
  p_reason text,
  p_requested_by text,
  p_dry_run boolean default true
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_request_id bigint;
  v_retained_rank integer;
  v_count integer;
begin
  if p_retained_plan is not null then
    v_retained_rank := market.plan_rank(p_provider_id, p_retained_plan);
    if v_retained_rank is null then
      raise exception 'unknown plan % for provider %', p_retained_plan, p_provider_id;
    end if;
  end if;

  insert into market.purge_requests
    (provider_id, dataset_key, retained_plan, reason, requested_by, dry_run)
  values (p_provider_id, p_dataset_key, p_retained_plan, p_reason, p_requested_by, p_dry_run)
  returning purge_request_id into v_request_id;

  -- Everything still held for this scope that the retained plan does not cover.
  -- With no retained plan, that is everything.
  insert into market.purge_targets (purge_request_id, object_key, store_id)
  select v_request_id, o.object_key, o.store_id
  from market.raw_objects o
  where o.provider_id = p_provider_id
    and (p_dataset_key is null or o.dataset_key = p_dataset_key)
    and o.purged_at is null
    and (
      v_retained_rank is null
      or coalesce(market.plan_rank(o.provider_id, o.required_min_plan), 2147483647) > v_retained_rank
    );

  get diagnostics v_count = row_count;
  update market.purge_requests set target_count = v_count where purge_request_id = v_request_id;
  return v_request_id;
end;
$$;

comment on function market.open_purge_request(text, text, text, text, text, boolean) is
  'Enumerates every live object a licence purge must delete and returns the request id. Enumeration and deletion are separate steps so the list can be reviewed - and so a dry run is a real rehearsal rather than a different code path.';

create or replace function market.record_purge_result(
  p_purge_request_id bigint,
  p_object_key text,
  p_status market.purge_status,
  p_detail text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dry_run boolean;
begin
  select dry_run into v_dry_run from market.purge_requests where purge_request_id = p_purge_request_id;
  if not found then
    raise exception 'no such purge request %', p_purge_request_id;
  end if;

  update market.purge_targets
  set status = p_status, detail = p_detail, resolved_at = clock_timestamp()
  where purge_request_id = p_purge_request_id and object_key = p_object_key;

  if not found then
    raise exception 'object % is not a target of purge request %', p_object_key, p_purge_request_id;
  end if;

  -- A dry run rehearses the enumeration, never the deletion, so it must not
  -- mark the manifest as purged.
  if not v_dry_run and p_status in ('DELETED', 'NOT_FOUND') then
    update market.raw_objects
    set purged_at = clock_timestamp(), purge_request_id = p_purge_request_id
    where object_key = p_object_key and purged_at is null;
  end if;
end;
$$;

create or replace function market.complete_purge_request(p_purge_request_id bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_pending integer;
  v_failed integer;
  v_skipped integer;
  v_deleted integer;
  v_total integer;
  v_dry_run boolean;
  v_complete boolean;
begin
  select dry_run into v_dry_run from market.purge_requests where purge_request_id = p_purge_request_id;
  if not found then
    raise exception 'no such purge request %', p_purge_request_id;
  end if;

  select count(*) filter (where status = 'PENDING'),
         count(*) filter (where status = 'FAILED'),
         count(*) filter (where status = 'SKIPPED'),
         count(*) filter (where status in ('DELETED', 'NOT_FOUND')),
         count(*)
    into v_pending, v_failed, v_skipped, v_deleted, v_total
  from market.purge_targets where purge_request_id = p_purge_request_id;

  -- A rehearsal is never an obligation met. A dry run stays open however many
  -- targets it walked, so it cannot be mistaken for a purge that happened.
  v_complete := not v_dry_run and v_pending = 0 and v_failed = 0 and v_skipped = 0;

  update market.purge_requests
  set completed_at = case when v_complete then clock_timestamp() else null end,
      deleted_count = v_deleted
  where purge_request_id = p_purge_request_id;

  return jsonb_build_object(
    'purge_request_id', p_purge_request_id,
    'dry_run', v_dry_run,
    'targets', v_total,
    'deleted', v_deleted,
    'pending', v_pending,
    'skipped', v_skipped,
    'failed', v_failed,
    'complete', v_complete
  );
end;
$$;

comment on function market.complete_purge_request(bigint) is
  'Closes a purge only when it actually deleted something and every target is resolved. A dry run, or a request with pending, skipped or failed targets, stays open - an unmet deletion obligation should look unmet.';

create or replace view market.outstanding_purge_obligations as
  select r.purge_request_id,
         r.provider_id,
         r.dataset_key,
         r.retained_plan,
         r.reason,
         r.requested_at,
         r.dry_run,
         count(*) filter (where t.status = 'PENDING') as pending,
         count(*) filter (where t.status = 'FAILED') as failed
  from market.purge_requests r
  join market.purge_targets t using (purge_request_id)
  where r.completed_at is null
  group by r.purge_request_id
  order by r.requested_at;

comment on view market.outstanding_purge_obligations is
  'Purges that have not finished. This should normally be empty; a row here is a licence obligation we have not met.';

-- ----------------------------------------------------------------- privileges
revoke all on table market.raw_objects, market.purge_requests, market.purge_targets,
  market.provider_plans from public;

grant select on table market.provider_plans, market.raw_objects
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant insert on table market.raw_objects to surge_worker_prod;
grant select on table market.purge_requests, market.purge_targets
  to surge_worker_prod, surge_worker_research, surge_readonly;
grant select on market.outstanding_purge_obligations
  to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;

revoke all on function
  market.open_purge_request(text, text, text, text, text, boolean),
  market.record_purge_result(bigint, text, market.purge_status, text),
  market.complete_purge_request(bigint)
from public;

-- The purge principal, and nobody else. surge_worker_prod is deliberately absent.
grant execute on function
  market.open_purge_request(text, text, text, text, text, boolean),
  market.record_purge_result(bigint, text, market.purge_status, text),
  market.complete_purge_request(bigint)
to surge_purge;

grant execute on function market.plan_rank(text, text)
  to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;
