# Checking the cloud schema against `supabase/migrations/`

`supabase/migrations/` is the only source of truth for the schema (CLAUDE.md
§4). This is how that rule is verified rather than assumed.

## What CI proves, and what it does not

The `migrations-and-db-tests` job starts an empty Postgres 17 and applies every
file in `supabase/migrations/` to it. The drift test then compares that database
against those same files. That comparison is very nearly a tautology: the only
thing it can fail on is a bug in the parser, where a definition the files
contain is not the one the check extracts.

That is worth having - it caught exactly such a bug on 2026-09-18, where the
newest definition of `pipeline.snapshot_securities` uses a plain `create
function` (its signature changed, so `or replace` was not possible) and the
regex only recognised `create or replace function`. The check was reporting the
database as drifted for holding the correct body.

But it is not the divergence the rule exists to prevent. **CI never opens the
cloud database.** The divergence happens there, when DDL is applied from a paste
rather than from the file.

## What found the real drift

```bash
SURGE_DATABASE_URL='<the cloud connection string>' python -m surge.jobs.schema_drift_cli
```

Read-only: it opens a read-only transaction, reads `pg_proc` for the ten project
schemas, and rolls back. Exit code 0 when the schema matches, 1 when it does
not, 2 when the question could not be asked. `--json` for machine-readable
output.

Run against the cloud on 2026-09-18 it reported ten drifted functions -
`market.complete_purge_request`, `market.forbid_license_policy_rewrite`,
`market.open_purge_request`, `market.purge_market_rows`,
`market.record_purge_result`, `news.forbid_policy_mutation`,
`news.purge_documents`, `pipeline.required_sources`, `pipeline.validate_run`,
`ref.log_provenance_correction` - every one of them a body whose `--` comments
had been stripped on the way in, and one unmanaged function
(`ref.tmp_trigger_probe`, a leftover `select 1` probe) that no migration
defined.

Behaviour was identical in every case. Only the text differed, which is exactly
why nothing else noticed: the tests passed, the guards fired, and the file
explained reasoning the database did not carry.

## Two directions, two names

- **drifted** - both define the function, and the bodies differ.
- **unmanaged** - the database has a function in a project schema that no
  migration defines at all. Not "the file says something else" but "no file says
  anything", which is how a hand-made object survives unnoticed.

## How to repair drift

The migrations are the source of truth, so **the database is what changes**.
Re-apply the drifted function from its file - not from a retyped copy, which is
how it drifted in the first place - then re-run the check. Do not edit the
migration to match the database.

If a function is unmanaged, either add the migration that creates it or drop it.
`20260918000000_drop_probe_function.sql` is an example of the second.

## Why this is not wired into CI

Pointing CI at the cloud database would mean putting a production connection
string into GitHub Secrets, and giving every pull request build a path to the
real database. The check is read-only, but the credential would not be. So this
stays a command run against the cloud deliberately, and the readiness report
runs the same comparison through the connection it already has
(`migrations_in_sync`, `readiness-1.5.0`) - which means it is now measured
rather than asserted with a flag. The `--migrations-in-sync` flag is gone: a
command line saying the schema matches never made it match.
