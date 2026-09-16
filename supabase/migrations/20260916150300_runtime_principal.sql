-- Phase 1.1: a real least privilege principal for the production worker.
--
-- Until now surge_worker_prod was a NOLOGIN group role whose only member was the
-- database owner, so nothing could actually connect with least privilege. This
-- creates a LOGIN role that is a member of surge_worker_prod. The password is
-- generated inside the database and stored in Supabase Vault: it is never
-- printed, returned, logged or committed.
--
-- Where Vault is unavailable (plain Postgres, CI), the role is not created here;
-- the CI workflow creates its own throwaway principal for the access tests.

do $$
declare
  v_password text;
  v_database text := current_database();
begin
  if not exists (select 1 from pg_namespace where nspname = 'vault') then
    raise notice 'vault unavailable: runtime login role not created in this database';
    return;
  end if;

  if exists (select 1 from pg_roles where rolname = 'surge_worker_prod_app') then
    raise notice 'runtime login role already exists; password left untouched';
    return;
  end if;

  v_password := encode(extensions.gen_random_bytes(32), 'base64');

  execute format('create role surge_worker_prod_app login password %L', v_password);
  execute 'grant surge_worker_prod to surge_worker_prod_app';
  execute format('grant connect on database %I to surge_worker_prod_app', v_database);

  perform vault.create_secret(
    v_password,
    'surge_worker_prod_app_password',
    'Least privilege database password for the production worker. Read it from Vault when configuring a worker; never commit it.'
  );

  v_password := null;
end
$$;
