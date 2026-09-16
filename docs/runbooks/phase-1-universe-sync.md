# Runbook: security master + universe sync (Phase 1)

## What the job does

1. Fetches an official security master snapshot for one market.
2. Resolves issuer and security identity from stable registry identifiers
   (SEC CIK, EDINET code, JPX local code). A ticker is never an identity.
3. Normalises every record and classifies it against `universe-1.0.0`.
4. Writes SQL artefacts: the run row, provenance rows, the snapshot rows and the
   apply statements that materialise the master, the evaluations and coverage.

The job never writes provider data into the repository. Artefacts are written to
a local directory that is git-ignored.

## Sources

| Market | Source | Notes |
|---|---|---|
| JP | JPX listed issue workbook (`data_j.xlsx`) | Carries JPX's own market / product classification. No credentials. |
| JP | EDINET code list (`Edinetcode.zip`) | Exchange securities code → EDINET code and corporate number. No credentials. |
| US | Nasdaq Trader Symbol Directory (`nasdaqlisted.txt`, `otherlisted.txt`) | Exchange, ETF flag, test issue flag. No credentials. |
| US | SEC `company_tickers_exchange.json` | ticker → CIK. Requires a declared User-Agent. |
| US | SEC EDGAR company list by SIC | SIC 6770 (blank check) and 6798 (REIT) membership. |

## Identity

| Level | Key | Confidence |
|---|---|---|
| Issuer, US | `CIK:<cik>` | STRONG |
| Issuer, JP | `EDINET:<code>` | STRONG |
| Issuer, fallback | `NAME:<market>:<normalized name>` | PROVISIONAL |
| Security, JP | `JP:JPX:<local code>` | STRONG |
| Security, US with CIK | `US:CIK:<cik>:<type>:<class token>` | STRONG |
| Security, US fallback | `US:<exchange>:SYMBOL:<symbol>` | PROVISIONAL |
| Listing | `<exchange>` + the security identity key | follows the security |

Two records that resolve to the same strong security key but are different
instruments are both demoted to the provisional key and reported as a provider
warning; they are never merged. A ticker change is ticker history on the same
listing, not a new security.

## Run it

```bash
cd workers
python -m pip install -e ".[dev]"

export PYTHONPATH="$PWD/src"
export SURGE_CONTACT_EMAIL="you@example.com"   # goes into the SEC User-Agent

python -m surge.cli universe-sync --market JP --out ../.local/phase1
python -m surge.cli universe-sync --market US --out ../.local/phase1
```

Output per market:

| File | Contents |
|---|---|
| `<market>_00_run.sql` | `pipeline.runs`, `pipeline.source_fetches`, `pipeline.run_errors` |
| `<market>_snapshot.tsv` | the snapshot itself, unit separator (`0x1f`) delimited, 33 fields per line |
| `<market>_99_apply.sql` | `ref.apply_master_snapshot`, `universe.apply_snapshot_evaluations`, `universe.compute_coverage`, run completion |
| `<market>_summary.json` | counts, decision and reason breakdown, identity breakdown, source provenance |

## Load it

Bulk rows go to object storage and the database reads them from there, so the
snapshot never has to travel through the orchestration layer.

The loader only accepts a **short lived signed URL** on an **allowlisted host**
over **https**. No API key is ever passed to, stored in or sent by the database,
and the database sends no `Authorization` header to anything.

Register the storage host once per environment (it is deployment configuration,
not schema, so no migration seeds it):

```sql
insert into pipeline.load_host_allowlist (host, note)
values ('<project ref>.supabase.co', 'project object storage; signed URLs only')
on conflict (host) do nothing;
```

```bash
# 1. run row and provenance
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f jp_00_run.sql

# 2. upload the snapshot to the private bucket
curl -sS -X POST "$SUPABASE_URL/storage/v1/object/phase1-load/jp_snapshot.tsv" \
  -H "apikey: $SUPABASE_PUBLISHABLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_PUBLISHABLE_KEY" \
  -H "Content-Type: text/tab-separated-values" \
  --data-binary @jp_snapshot.tsv

# 3. mint a signed URL with a short expiry and keep it in a shell variable
SIGNED_PATH=$(curl -sS -X POST "$SUPABASE_URL/storage/v1/object/sign/phase1-load/jp_snapshot.tsv" \
  -H "apikey: $SUPABASE_PUBLISHABLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_PUBLISHABLE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"expiresIn":300}' | python -c "import json,sys; print(json.load(sys.stdin)['signedURL'])")

# 4. let the database read it, then materialise
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -c \
  "select pipeline.load_master_snapshot_from_signed_url('<run id>'::uuid,
     '$SUPABASE_URL/storage/v1$SIGNED_PATH');"
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f jp_99_apply.sql

# 5. delete the uploaded object once the load is verified
```

`SUPABASE_DB_URL` and the keys come from the environment or a local `.env`; they
are never committed and never printed. The signed URL is itself a short lived
credential: keep it in a variable, do not log it, and let it expire.

If the upload is done with a publishable key, the storage policies that allow it
are temporary: create them before the upload and drop them immediately after, so
the bucket has no standing anonymous access.

```sql
create policy phase1_load_anon_insert on storage.objects for insert to anon with check (bucket_id = 'phase1-load');
create policy phase1_load_anon_select on storage.objects for select to anon using (bucket_id = 'phase1-load');
create policy phase1_load_anon_delete on storage.objects for delete to anon using (bucket_id = 'phase1-load');
-- ... upload, sign, load, delete the object, then:
drop policy phase1_load_anon_insert on storage.objects;
drop policy phase1_load_anon_select on storage.objects;
drop policy phase1_load_anon_delete on storage.objects;
```

Re-running a load is safe: identities are deterministic, history rows are only
opened when a value actually changed, and an unchanged re-observation only moves
`last_confirmed_at` forward.

## Who connects

The production worker connects as `surge_worker_prod_app`, a LOGIN role that is a
member of the `surge_worker_prod` group. Its password is generated inside the
database by `20260916150300_runtime_principal.sql` and stored in Supabase Vault;
read it from Vault when configuring a worker. The role has no `DELETE` on the
history tables and no access to the `research` schema.

## Rebuilding the master

A rebuild is only for identity changes that cannot be migrated in place, and it
has to stay auditable:

1. `ref.identity_migration_map` records every old security / listing / issuer id
   with its provider coordinates (`20260916150400`).
2. The master, universe evaluations, coverage and staging rows are cleared.
   `pipeline.runs` and `pipeline.source_fetches` are kept: they are the record of
   what was ingested and when.
3. The job is re-run from the official sources and loaded as above.
4. `20260916150500` fills `new_security_id` / `new_listing_id` / `new_issuer_id`
   so old → new can be reconstructed.

## Reading the result

```sql
-- coverage for the latest run of a market
select scope_kind, scope_value, retrieved_count, included_count, excluded_count, unresolved_count
from universe.coverage
where market_code = 'JP'
order by computed_at desc, scope_kind, scope_value;

-- why a security was excluded
select e.decision, e.reason_code, e.decision_detail
from universe.evaluations e
join ref.securities s using (security_id)
where s.identity_key = 'JP:JPX:1301';

-- the master as it was known at a point in time
select * from ref.listings_as_of(timestamptz '2026-09-16 12:00+09');

-- old (Phase 1) id -> new id
select old_security_id, new_security_id, old_issuer_id, new_issuer_id
from ref.identity_migration_map
where migration_label = 'phase-1.1-identity-rebuild' and local_code = 'AAPL';
```
