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
| Issuer, fallback | `ISSUER-OF:<security identity key>` | PROVISIONAL |
| Security, JP | `JP:JPX:<local code>` | STRONG |
| Security, US with CIK | `US:CIK:<cik>:<type>:<class token>` | REGISTRY_ANCHORED |
| Security, US fallback | `US:<exchange>:SYMBOL:<symbol>` | PROVISIONAL |
| Listing | `<exchange>` + the security identity key | follows the security |

`STRONG` means a registry issued the identifier for that thing. The US security
key mixes a CIK with a type and a share class read out of the provider's display
name, so a formatting change can move it: it is `REGISTRY_ANCHORED`, never
`STRONG`. Nothing is promoted automatically.

The fallback issuer key follows the security's own coordinate and never a name:
two companies whose names normalise to the same string stay separate issuers,
because a false split can be merged later and a false merge cannot be undone.
The provider's name is kept as an `ALIAS` row in `ref.issuer_names`.

Two records that resolve to the same reusable security key but are different
instruments are both demoted to the provisional key and reported with
`error_type = 'IDENTITY_COLLISION'`; they are never merged. A ticker change is
ticker history on the same listing, not a new security.

## Run it

```bash
cd workers
python -m pip install -e ".[dev]"

export PYTHONPATH="$PWD/src"
export SURGE_CONTACT_EMAIL="you@example.com"   # goes into the SEC User-Agent

# A PRODUCTION run must be attributable to a commit; the CLI refuses without it.
python -m surge.cli universe-sync --market JP --out ../.local/phase1 --git-sha "$(git rev-parse HEAD)"
python -m surge.cli universe-sync --market US --out ../.local/phase1 --git-sha "$(git rev-parse HEAD)"

# for a local experiment, say so explicitly instead
python -m surge.cli universe-sync --market JP --out ../.local/scratch --run-mode DEV
```

Current versions (must agree in the runbook, the summary JSON and the database):

| Version | Value |
|---|---|
| `job_version` | `universe_sync-1.1b.0` (`surge.JOB_VERSION`) |
| `identity_version` | `identity-1.1a` (`surge.identity.IDENTITY_VERSION`) |
| `universe_version` | `universe-1.0.0` |
| `git_sha` | the commit passed to `--git-sha`; required for `PRODUCTION` |
| `config_hash` | sha256 of the run configuration, written to `pipeline.runs.config_hash` |

Output per market:

| File | Contents |
|---|---|
| `<market>_00_run.sql` | `pipeline.runs`, `pipeline.source_fetches`, `pipeline.run_errors` |
| `<market>_snapshot.tsv` | the snapshot itself, unit separator (`0x1f`) delimited, 37 fields per line |
| `<market>_99_apply.sql` | `ref.apply_master_snapshot`, `universe.apply_snapshot_evaluations`, `universe.compute_coverage`, run completion |
| `<market>_summary.json` | counts, decision and reason breakdown, identity breakdown, source provenance |

## Load it

The snapshot goes into `pipeline.master_snapshot` and everything else is derived
from there. Two paths, one credential model: **the only secret is a connection
string or a bucket-scoped credential, and it lives in the worker secret store.**
No anonymous storage policy is ever created (Phase 1.1b).

### Direct load (the default)

```bash
# 1. run row, provenance, diagnostics, and the snapshot itself
SURGE_DB_URL=... python scripts/load_snapshot_direct.py ../.local/phase1 JP

# 2. materialise the master, the evaluations and coverage
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -f ../.local/phase1/jp_99_apply.sql

# 3. validate and publish (see "Which run is the universe")
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c "select pipeline.publish_run('<run id>'::uuid);"
```

`scripts/rebuild_security_master.sh <market> <git sha> [label]` runs the whole
sequence.

### Object storage (when the job cannot reach the database)

The database can fetch the snapshot itself, and the loader accepts **only** a
short lived signed URL on an **allowlisted host** over **https**. Since Phase
1.1b the loader is `SECURITY DEFINER`: the privilege to make an HTTP request
belongs to the function, which checks the URL first, and no runtime role can
reach `extensions.http` at all.

Register the storage host once per environment (deployment configuration, so no
migration seeds it):

```sql
insert into pipeline.load_host_allowlist (host, note)
values ('<project ref>.supabase.co', 'project object storage; signed URLs only')
on conflict (host) do nothing;
```

The upload and the signing use a **bucket-scoped credential from the worker
secret store** (an S3-compatible scoped key, or a service credential held only
there). It is never committed, never printed, never given to a browser, and
never replaced by a temporary anonymous policy on the bucket:

```bash
# upload with the worker's own credential, mint a short signed URL, load, delete
python scripts/upload_and_sign.py ../.local/phase1/jp_snapshot.tsv   # writes $SIGNED_URL
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c   "select pipeline.load_master_snapshot_from_signed_url('<run id>'::uuid, '$SIGNED_URL');"
```

The database only ever learns the signed URL, and that URL expires. If a
provider or a bucket cannot issue a scoped credential, use the direct path
instead - do not open the bucket to anonymous access to work around it.

## Who may write what

The runtime worker connects as `surge_worker_prod_app` (a member of
`surge_worker_prod`) and may write only its own output:

| May write | May only read |
|---|---|
| `pipeline.runs`, `pipeline.source_fetches`, `pipeline.run_errors`, `pipeline.master_snapshot`, `ref.issuers`, `ref.issuer_names`, `ref.securities`, `ref.listings`, `ref.listing_states`, `ref.listing_symbols`, `ref.security_names`, `ref.security_identifiers`, `universe.evaluations`, `universe.coverage` | `pipeline.load_host_allowlist`, `pipeline.sources`, `ref.exchanges`, `ref.identity_migration_map`, `universe.definitions`, `universe.decision_reasons`, and everything in `research` |

`DELETE` is granted to the worker nowhere, including `prod`. It also needs `USAGE` on schema `extensions` to run the loader, which is SECURITY INVOKER (`20260916160800`). Since `20260916160000` the default
privileges in `ref` / `pipeline` / `universe` grant SELECT only, so **a migration
that adds a runtime table must grant write explicitly** - a new configuration
table is read-only unless someone says otherwise. That is how the allowlist
became writable in the first place.

## Upgrade procedure (an existing database)

Applying the migrations does **not** by itself migrate an existing master: the
rebuild migration records the old identifiers and clears the master, and the new
identifiers only exist after the providers have been re-fetched. Run, per market:

```bash
SURGE_DB_URL=... scripts/rebuild_security_master.sh JP "$(git rev-parse HEAD)" phase-1.1a-identity-rebuild
SURGE_DB_URL=... scripts/rebuild_security_master.sh US "$(git rev-parse HEAD)" phase-1.1a-identity-rebuild
```

The script ends with `ref.finalize_identity_rebuild('<label>')`, which fills the
old -> new id map and **refuses to run while the master is empty**, so a plain
`db push` can never look like a completed migration.

Without a label the same script is an ordinary refresh: fetch, load, apply,
validate, publish.

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
