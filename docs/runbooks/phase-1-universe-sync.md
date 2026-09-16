# Runbook: security master + universe sync (Phase 1)

## What the job does

1. Fetches an official security master snapshot for one market.
2. Normalises every record and classifies it against `universe-1.0.0`.
3. Writes SQL artefacts: the run row, provenance rows, the snapshot rows and the
   apply statements that materialise the master, the evaluations and coverage.

The job never writes provider data into the repository. Artefacts are written to
a local directory that is git-ignored.

## Sources

| Market | Source | Notes |
|---|---|---|
| JP | JPX listed issue workbook (`data_j.xlsx`) | Carries JPX's own market / product classification. No credentials. |
| US | Nasdaq Trader Symbol Directory (`nasdaqlisted.txt`, `otherlisted.txt`) | Exchange, ETF flag, test issue flag. No credentials. |
| US | SEC `company_tickers_exchange.json` | ticker → CIK. Requires a declared User-Agent. |
| US | SEC EDGAR company list by SIC | SIC 6770 (blank check) and 6798 (REIT) membership. |

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
| `<market>_snapshot.tsv` | the snapshot itself, unit separator (`0x1f`) delimited, 25 fields per line |
| `<market>_99_apply.sql` | `ref.apply_master_snapshot`, `universe.apply_snapshot_evaluations`, `universe.compute_coverage`, run completion |
| `<market>_summary.json` | counts, decision and reason breakdown, source provenance |

## Load it

Bulk rows go to object storage and the database reads them from there, so the
snapshot never has to travel through the orchestration layer.

```bash
# 1. run row and provenance
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f jp_00_run.sql

# 2. upload the snapshot to a private bucket
curl -sS -X POST "$SUPABASE_URL/storage/v1/object/phase1-load/jp_snapshot.tsv" \
  -H "apikey: $SUPABASE_PUBLISHABLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_PUBLISHABLE_KEY" \
  -H "Content-Type: text/plain" \
  --data-binary @jp_snapshot.tsv

# 3. let the database read it, then materialise
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -c \
  "select pipeline.load_master_snapshot_from_url('<run id>'::uuid,
     '$SUPABASE_URL/storage/v1/object/phase1-load/jp_snapshot.tsv',
     '$SUPABASE_PUBLISHABLE_KEY');"
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f jp_99_apply.sql

# 4. remove the uploaded file once the load is verified
```

`SUPABASE_DB_URL` and the keys come from the environment or a local `.env`; they
are never committed and never printed. The load bucket is private, the uploaded
file is deleted after the load, and an alternative is to run the same three
statements from any host that can reach the database directly.

If the upload is done with a publishable key, the storage policies that allow it
are temporary: create them before the upload and drop them immediately after, so
the bucket has no standing anonymous access.

```sql
create policy phase1_load_anon_insert on storage.objects for insert to anon with check (bucket_id = 'phase1-load');
create policy phase1_load_anon_select on storage.objects for select to anon using (bucket_id = 'phase1-load');
create policy phase1_load_anon_delete on storage.objects for delete to anon using (bucket_id = 'phase1-load');
-- ... upload, load, delete the object, then:
drop policy phase1_load_anon_insert on storage.objects;
drop policy phase1_load_anon_select on storage.objects;
drop policy phase1_load_anon_delete on storage.objects;
```

Re-running a load is safe: identities are deterministic and every insert is
either `on conflict do nothing` or an explicit upsert.

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
join ref.listings l using (listing_id)
where l.local_code = '13010';

-- the master as it was known at a point in time
select * from ref.listings_as_of(timestamptz '2026-09-16 12:00+09');
```
