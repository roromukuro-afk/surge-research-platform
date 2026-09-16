#!/usr/bin/env bash
# Rebuild or refresh the security master from the official sources, for one market.
#
# Applying the migrations alone does NOT migrate an existing database: the
# rebuild migration records the old identifiers and clears the master, and the
# new identifiers only exist once the providers have been re-fetched and the
# snapshot reloaded. This script is that sequence, in order.
#
# Credentials (Phase 1.1b): everything here needs exactly one secret, the
# database DSN, and it comes from the environment / the worker secret store. No
# object storage, no signed URL, no anonymous storage policy. Where the machine
# running the job cannot reach the database directly, use the object storage
# path in docs/runbooks/phase-1-universe-sync.md with a bucket-scoped credential
# from the same secret store.
#
# Usage:
#   SURGE_DB_URL=postgresql://...  scripts/rebuild_security_master.sh JP <git sha> [label] [out dir]

set -euo pipefail

MARKET="${1:?market (JP|US)}"
GIT_SHA="${2:?git sha of the code producing this run}"
LABEL="${3:-}"                       # identity migration label, only for a rebuild
OUT_DIR="${4:-./.local/rebuild}"     # git-ignored; never inside a tracked directory
UNIVERSE_VERSION="${UNIVERSE_VERSION:-universe-1.0.0}"

: "${SURGE_DB_URL:?set SURGE_DB_URL (read from the environment, never printed)}"

lower_market="$(echo "$MARKET" | tr '[:upper:]' '[:lower:]')"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"   # absolute: the job runs from workers/

echo "1/5 fetching ${MARKET} from the official sources"
(cd workers && PYTHONPATH=src python -m surge.cli universe-sync \
  --market "$MARKET" --out "$OUT_DIR" --git-sha "$GIT_SHA")

run_id="$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['run_id'])" \
  "${OUT_DIR}/${lower_market}_summary.json")"
echo "    run_id=${run_id}"

echo "2/5 recording the run and loading the snapshot (direct COPY, no storage)"
python scripts/load_snapshot_direct.py "$OUT_DIR" "$MARKET"

echo "3/5 materialising the master, the evaluations and coverage"
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -f "${OUT_DIR}/${lower_market}_99_apply.sql"

if [ -n "$LABEL" ]; then
  echo "4/5 finalizing the identity map for ${LABEL}"
  psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c "select ref.finalize_identity_rebuild('${LABEL}');"
else
  echo "4/5 no identity migration label given: skipping the old -> new id fill"
fi

echo "5/5 validating and publishing the run"
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c "select pipeline.validate_run('${run_id}'::uuid);"
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c "select pipeline.publish_run('${run_id}'::uuid);"
psql "$SURGE_DB_URL" -v ON_ERROR_STOP=1 -c \
  "select universe.authoritative_run_at('${MARKET}', '${UNIVERSE_VERSION}') as authoritative_run;"

echo "done: ${MARKET} refreshed and published as ${run_id}"
