-- Phase 1: production/research separation, grants and row level security.

-- Placeholder tables so the separation is enforceable and testable from Phase 1 (RF-15).
create table prod.access_guard (
  guard_id     text primary key,
  note         text not null,
  created_at   timestamptz not null default now()
);
comment on table prod.access_guard is 'Placeholder production table. Research roles must never be able to write here.';

create table research.access_guard (
  guard_id     text primary key,
  note         text not null,
  created_at   timestamptz not null default now()
);
comment on table research.access_guard is 'Placeholder research table. Research jobs may write here; production results never live here.';

insert into prod.access_guard (guard_id, note)
values ('phase-1', 'Production schema reserved for production-only outputs.')
on conflict (guard_id) do nothing;

insert into research.access_guard (guard_id, note)
values ('phase-1', 'Research schema reserved for replay / research outputs.')
on conflict (guard_id) do nothing;

-- ------------------------------------------------------------------ grants
grant select, insert, update on all tables in schema ref, pipeline, universe to surge_worker_prod;
grant select, insert, update, delete on all tables in schema prod to surge_worker_prod;
grant select on all tables in schema research to surge_worker_prod;

grant select on all tables in schema ref, pipeline, universe to surge_worker_research, surge_readonly;
grant select, insert, update, delete on all tables in schema research to surge_worker_research;
grant select on all tables in schema prod to surge_readonly;

grant execute on function ref.listings_as_of(timestamptz)
  to surge_worker_prod, surge_worker_research, surge_readonly;

alter default privileges in schema ref, pipeline, universe
  grant select, insert, update on tables to surge_worker_prod;
alter default privileges in schema ref, pipeline, universe
  grant select on tables to surge_worker_research, surge_readonly;
alter default privileges in schema prod
  grant select, insert, update, delete on tables to surge_worker_prod;
alter default privileges in schema prod
  grant select on tables to surge_readonly;
alter default privileges in schema research
  grant select, insert, update, delete on tables to surge_worker_research;
alter default privileges in schema research
  grant select on tables to surge_worker_prod, surge_readonly;

-- History tables are append-only for every application role: no DELETE is granted anywhere
-- in ref/pipeline/universe, so past tickers, delistings and evaluations cannot be removed.

-- --------------------------------------------------------------------- RLS
-- Every table gets RLS with explicit policies. Anonymous and authenticated
-- Supabase roles get no policy at all, so they can read nothing.
do $$
declare
  t record;
begin
  for t in
    select schemaname, tablename
    from pg_tables
    where schemaname in ('ref', 'pipeline', 'universe', 'prod', 'research')
  loop
    execute format('alter table %I.%I enable row level security', t.schemaname, t.tablename);

    if not exists (select 1 from pg_policies p
                   where p.schemaname = t.schemaname and p.tablename = t.tablename
                     and p.policyname = 'surge_worker_prod_all') then
      execute format(
        'create policy surge_worker_prod_all on %I.%I as permissive for all to surge_worker_prod using (true) with check (true)',
        t.schemaname, t.tablename);
    end if;

    if t.schemaname = 'research' then
      if not exists (select 1 from pg_policies p
                     where p.schemaname = t.schemaname and p.tablename = t.tablename
                       and p.policyname = 'surge_worker_research_all') then
        execute format(
          'create policy surge_worker_research_all on %I.%I as permissive for all to surge_worker_research using (true) with check (true)',
          t.schemaname, t.tablename);
      end if;
    elsif t.schemaname <> 'prod' then
      if not exists (select 1 from pg_policies p
                     where p.schemaname = t.schemaname and p.tablename = t.tablename
                       and p.policyname = 'surge_worker_research_read') then
        execute format(
          'create policy surge_worker_research_read on %I.%I as permissive for select to surge_worker_research using (true)',
          t.schemaname, t.tablename);
      end if;
    end if;

    if not exists (select 1 from pg_policies p
                   where p.schemaname = t.schemaname and p.tablename = t.tablename
                     and p.policyname = 'surge_readonly_read') then
      execute format(
        'create policy surge_readonly_read on %I.%I as permissive for select to surge_readonly using (true)',
        t.schemaname, t.tablename);
    end if;
  end loop;
end
$$;
