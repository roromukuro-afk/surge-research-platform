#!/usr/bin/env bash
# Rebuild the security master from the official sources.
#
# Applying the migrations alone does NOT migrate an existing database: the
# rebuild migration records the old identifiers and clears the master, and the
# new identifiers only exist once the providers have been re-fetched and the
# snapshot reloaded. This script is that sequence, in order, for one market.
#
# Usage:
#   SUPABASE_DB_URL=...   SUPABASE_URL=...   SUPABASE_PUBLISHABLE_KEY=... \
#   scripts/rebuild_security_master.sh JP phase-1.1a-identity-rebuild ./.local/rebuild
#
# The load bucket must exist and be private, its host must be registered in
# pipeline.load_host_allowlist, and the temporary storage policies must be
# created before and dropped after (see docs/runbooks/phase-1-universe-sync.md).
# No key is ever passed to the database: only a short lived signed URL.

set -euo pipefail

MARKET="${1:?market (JP|US)}"
LABEL="${2:?identity migration label, e.g. phase-1.1a-identity-rebuild}"
OUT_DIR="${3:-./.local/rebuild}"
BUCKET="${SNAPSHOT_BUCKET:-phase1-load}"
EXPIRES="${SIGNED_URL_TTL:-300}"

: "${SUPABASE_DB_URL:?set SUPABASE_DB_URL}"
: "${SUPABASE_URL:?set SUPABASE_URL}"
: "${SUPABASE_PUBLISHABLE_KEY:?set SUPABASE_PUBLISHABLE_KEY}"

lower_market="$(echo "$MARKET" | tr '[:upper:]' '[:lower:]')"
object_path="rebuild/${lower_market}_snapshot.tsv"

echo "1/6 fetching ${MARKET} from the official sources"
(cd workers && PYTHONPATH=src python -m surge.cli universe-sync --market "$MARKET" --out "$(cd "$OLDPWD" && cd "$OUT_DIR" && pwd)")

run_id="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['run_id'])" \
  "${OUT_DIR}/${lower_market}_summary.json")"
echo "    run_id=${run_id}"

echo "2/6 recording the run and its provenance"
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "${OUT_DIR}/${lower_market}_00_run.sql"

echo "3/6 uploading the snapshot to the private bucket"
curl -sS -f -X POST "${SUPABASE_URL}/storage/v1/object/${BUCKET}/${object_path}" \
  -H "apikey: ${SUPABASE_PUBLISHABLE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_PUBLISHABLE_KEY}" \
  -H "Content-Type: text/tab-separated-values" \
  -H "x-upsert: true" \
  --data-binary "@${OUT_DIR}/${lower_market}_snapshot.tsv" > /dev/null

echo "4/6 loading it through a short lived signed URL"
signed_path="$(curl -sS -f -X POST "${SUPABASE_URL}/storage/v1/object/sign/${BUCKET}/${object_path}" \
  -H "apikey: ${SUPABASE_PUBLISHABLE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_PUBLISHABLE_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"expiresIn\":${EXPIRES}}" | python -c "import json,sys; print(json.load(sys.stdin)['signedURL'])")"
# The signed URL is a credential: it stays in this variable, is never echoed,
# and expires in ${EXPIRES} seconds.
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -q -c \
  "select pipeline.load_master_snapshot_from_signed_url('${run_id}'::uuid, '${SUPABASE_URL}/storage/v1${signed_path}') as rows_loaded;"

echo "5/6 materialising the master, the evaluations and coverage"
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "${OUT_DIR}/${lower_market}_99_apply.sql"

echo "6/6 removing the uploaded object and finalizing the identity map"
curl -sS -f -X DELETE "${SUPABASE_URL}/storage/v1/object/${BUCKET}/${object_path}" \
  -H "apikey: ${SUPABASE_PUBLISHABLE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_PUBLISHABLE_KEY}" > /dev/null
# Safe to run once per market: it only fills rows that are still unmapped, and
# it refuses to run while the master is empty.
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -c \
  "select ref.finalize_identity_rebuild('${LABEL}');"

echo "done: ${MARKET} rebuilt under ${LABEL}"
