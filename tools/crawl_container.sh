#!/usr/bin/env bash
# Build and run the registry crawl in a container, against a local state directory.
#
# The container never pushes: it writes into STATE_DIR, laid out exactly like the
# artifact-data branch.  Seeding from the published branch makes a local run resume
# the shared cursor rather than restart it; publishing back is a deliberate step
# (see docs/OPERATIONS.md), because CI writes the same branch.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-global-executables-crawl:local}"
STATE_DIR="${STATE_DIR:-${ROOT_DIR}/.local-crawl}"
SEED="${SEED:-1}"

cd "${ROOT_DIR}"
mkdir -p "${STATE_DIR}/data/production/intermediate" "${STATE_DIR}/reports"

if [ "${SEED}" = "1" ] && [ ! -f "${STATE_DIR}/data/production/registry-state.json" ]; then
  echo "==> Seeding ${STATE_DIR} from origin/artifact-data"
  git fetch origin artifact-data --quiet || true
  for path in data/production/registry-state.json \
              data/production/pypi-projects.txt \
              data/production/rubygems-names.txt \
              data/production/packagist-packages.txt \
              data/production/npm-packages.txt \
              data/production/npm-packages.txt.gz \
              reports/registry-artifact-crawl.json; do
    if git cat-file -e "origin/artifact-data:${path}" 2>/dev/null; then
      git show "origin/artifact-data:${path}" > "${STATE_DIR}/${path}"
      echo "    ${path}"
    fi
  done
  for source in npm pypi crates rubygems packagist; do
    path="data/production/intermediate/${source}.jsonl"
    if git cat-file -e "origin/artifact-data:${path}" 2>/dev/null; then
      git show "origin/artifact-data:${path}" > "${STATE_DIR}/${path}"
    fi
  done
fi

echo "==> Building ${IMAGE}"
docker build --file Dockerfile.crawl --tag "${IMAGE}" . >/dev/null

echo "==> Crawling into ${STATE_DIR}"
exec docker run --rm --init \
  --volume "${STATE_DIR}:/state" \
  --env "SOURCES=${SOURCES:-npm pypi crates rubygems packagist}" \
  --env "PACKAGE_BUDGET=${PACKAGE_BUDGET:-2000}" \
  --env "SOURCE_PACKAGE_BUDGETS=${SOURCE_PACKAGE_BUDGETS:-}" \
  --env "BYTE_BUDGET=${BYTE_BUDGET:-5000000000}" \
  --env "PASSES=${PASSES:-0}" \
  "${IMAGE}"
