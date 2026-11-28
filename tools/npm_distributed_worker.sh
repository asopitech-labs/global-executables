#!/usr/bin/env bash
# Claim, crawl, publish, and checkpoint one npm work unit through Git.
#
# There is deliberately no scheduler. Every independently running copy races to
# commit a reservation to artifact-data; a rejected push reloads the ledger and
# chooses from the remaining work.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PACKAGE_BUDGET="${PACKAGE_BUDGET:-10000}"
ACTOR="${ACTOR:-$(hostname)-$$}"
ACTOR="$(printf '%s' "${ACTOR}" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-128)"
remove_workdir=0
if [ -z "${WORK_BASE:-}" ]; then
  WORK_BASE="$(mktemp -d /tmp/ge-npm-worker.XXXXXX)"
  remove_workdir=1
fi
if [ "${KEEP_WORKDIR:-0}" = 1 ]; then remove_workdir=0; fi
REPORT="${WORK_BASE}-npm-registry/reports/npm-registry-crawl.json"

cleanup() {
  status=$?
  if [ "${claimed:-0}" = 1 ] && [ "${checkpointed:-0}" != 1 ]; then
    "${PYTHON}" "${ROOT_DIR}/tools/work_ledger.py" release \
      --source npm-registry --id "${ACTOR}" >/dev/null 2>&1 || true
  fi
  if [ "${remove_workdir}" = 1 ]; then
    rm -rf "${WORK_BASE}" "${WORK_BASE}-npm-registry"
  fi
  trap - EXIT
  exit "${status}"
}
trap cleanup EXIT

cd "${ROOT_DIR}"
export BASE="${WORK_BASE}"
export SOURCES=npm-registry
export OBSERVATION_SOURCES=" "
export DELTA_SOURCES=npm-registry

bash tools/crawl_parallel.sh seed
"${PYTHON}" tools/npm_full_catalog.py \
  --output "${WORK_BASE}/data/production/npm-packages.txt" \
  --checkpoint "${WORK_BASE}/data/production/npm-packages.cursor"
NO_CONTAINER=1 bash tools/crawl_parallel.sh start
# Catalogue refresh is also optimistic: equal concurrent snapshots collapse to the
# same branch content, while crawl results remain delta publications below.
BOOSTER=0 bash tools/crawl_parallel.sh publish

set +e
claim="$(${PYTHON} tools/work_ledger.py claim --source npm-registry --id "${ACTOR}" \
  --catalog "${WORK_BASE}/data/production/npm-packages.txt" \
  --manifest "${WORK_BASE}/data/production/npm-packages.txt.manifest.json" \
  --state "${WORK_BASE}/data/production/registry-state")"
claim_status=$?
set -e
printf '%s\n' "${claim}"
if [ "${claim_status}" -eq 3 ]; then
  echo "all npm work units are complete"
  exit 0
elif [ "${claim_status}" -ne 0 ]; then
  exit "${claim_status}"
fi
claimed=1

read -r unit position < <("${PYTHON}" -c \
  'import json,sys; v=json.load(sys.stdin); u=v["units"][0]; print(u, v["positions"][u])' <<<"${claim}")
bucket=$((16#${unit}))

worker_dir="${WORK_BASE}-npm-registry"
"${PYTHON}" tools/npm_registry_crawl.py \
  --catalog "${worker_dir}/data/production/npm-packages.txt" \
  --state "${worker_dir}/data/production/registry-state" \
  --output "${worker_dir}/data/production/intermediate/npm-registry.jsonl" \
  --report "${REPORT}" --budget "${PACKAGE_BUDGET}" \
  --bucket "${bucket}" --position "${position}"

publish_output="$(BOOSTER=1 BOOSTER_RANGES="${bucket}" bash tools/crawl_parallel.sh publish)"
printf '%s\n' "${publish_output}"
result_commit="$(printf '%s\n' "${publish_output}" | awk '/^published commit / {print $3}' | tail -1)"
if [ -z "${result_commit}" ]; then
  # A no-op still has durable evidence: the exact artifact-data commit read by Git.
  result_commit="$(git ls-remote origin refs/heads/artifact-data | awk '{print $1}')"
fi
read -r next_position failures complete < <("${PYTHON}" -c \
  'import json,sys; t=json.load(open(sys.argv[1]))["sources"]["npm-registry"]["task"]; print(t["position"], t["failures"], str(t["complete"]).lower())' \
  "${REPORT}")
complete_flag=()
if [ "${complete}" = true ]; then complete_flag=(--complete); fi
"${PYTHON}" tools/work_ledger.py checkpoint --source npm-registry --id "${ACTOR}" \
  --unit "${unit}" --position "${next_position}" --failures "${failures}" \
  --result-commit "${result_commit}" "${complete_flag[@]}"
checkpointed=1
echo "npm unit ${unit} checkpointed at ${next_position}; result ${result_commit}"
