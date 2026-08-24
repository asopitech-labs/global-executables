#!/usr/bin/env bash
# Run one crawl container per registry, so the sources stop queueing behind each other.
#
# Each registry answers from its own hosts, so crawling them serially spends most of
# the wall clock waiting on one of them.  Every container gets its own state directory
# — a shared one would have four writers racing over a single cursor file — and
# `merge` folds the per-source states back into one publishable tree.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-global-executables-crawl:local}"
GO_IMAGE="${GO_IMAGE:-global-executables-go-crawler:local}"
BASE="${BASE:-${HOME}/.ge-crawl}"
# An explicitly empty value is useful when publishing checkout observations only.
SOURCES="${SOURCES-pypi rubygems packagist nuget npm go}"
# Non-registry observations are produced in the checkout by production_crawl.py.  They
# share artifact-data as their durable source of truth even though they have no cursor
# state or per-source crawl container.
OBSERVATION_SOURCES="${OBSERVATION_SOURCES:-arch debian ubuntu homebrew msys2 scoop winget windows macos shell}"
PACKAGE_BUDGET="${PACKAGE_BUDGET:-3000}"
BYTE_BUDGET="${BYTE_BUDGET:-8000000000}"
PUBLISH_MAX_ATTEMPTS="${PUBLISH_MAX_ATTEMPTS:-3}"

# Catalogs are per source, so each container carries only the one it reads.
catalog_for() {
  case "$1" in
    pypi) echo "pypi-projects.txt pypi-projects.txt.gz" ;;
    rubygems) echo "rubygems-names.txt rubygems-names.txt.gz" ;;
    packagist) echo "packagist-packages.txt packagist-packages.txt.gz" ;;
    nuget) echo "nuget-tools.txt nuget-tools.txt.gz" ;;
    npm) echo "npm-packages.txt npm-packages.txt.gz" ;;
    # Go's catalogue is appended to page by page so an interrupted sweep resumes, so it
    # has no gzip twin: one left beside it goes stale the moment the sweep continues, and
    # `read_catalog` prefers the compressed copy — 993,468 stale names over 1.4M live ones.
    go) echo "go-modules.txt go-modules.cursor.json" ;;
    *) echo "" ;;
  esac
}

# `start` slices each container's state out of ${BASE}.  On a machine that has never run
# a crawl that file does not exist, so every source would begin at cursor zero and
# re-crawl what the branch already records.  Seeding is therefore part of starting.
seed() {
  cd "${ROOT_DIR}"
  mkdir -p "${BASE}/data/production/intermediate" "${BASE}/reports"
  git fetch origin artifact-data --quiet
  git show origin/artifact-data:data/production/registry-state.json \
    > "${BASE}/data/production/registry-state.json"
  for source in ${SOURCES}; do
    for name in $(catalog_for "${source}"); do
      if git cat-file -e "origin/artifact-data:data/production/${name}" 2>/dev/null; then
        git show "origin/artifact-data:data/production/${name}" > "${BASE}/data/production/${name}"
      fi
    done
    local rows="data/production/intermediate/${source}.jsonl"
    if git cat-file -e "origin/artifact-data:${rows}" 2>/dev/null; then
      git show "origin/artifact-data:${rows}" > "${BASE}/${rows}"
    fi
  done
  mkdir -p "${ROOT_DIR}/data/production/intermediate"
  for source in ${OBSERVATION_SOURCES}; do
    local rows="data/production/intermediate/${source}.jsonl"
    if [ ! -f "${ROOT_DIR}/${rows}" ] && git cat-file -e "origin/artifact-data:${rows}" 2>/dev/null; then
      git show "origin/artifact-data:${rows}" > "${ROOT_DIR}/${rows}"
    fi
  done
  # Report from the seeded base: the per-source directories `status` reads do not exist
  # until `start` slices them, so asking it here answers "cursor=None" for everything.
  echo "seeded ${BASE} from origin/artifact-data"
  python3 - "${BASE}" "${SOURCES}" <<'PY'
import json, pathlib, sys
base, sources = pathlib.Path(sys.argv[1]), sys.argv[2].split()
state = json.loads((base / "data/production/registry-state.json").read_text()).get("sources", {})
for source in sources:
    entry = state.get(source) or {}
    queued = next((len(v) for k, v in entry.items() if k.startswith("retry_") and isinstance(v, list)), 0)
    rows = base / f"data/production/intermediate/{source}.jsonl"
    counted = sum(1 for _ in rows.open()) if rows.is_file() else 0
    print(f"  {source:10} cursor={entry.get('cursor', 0):>9,}  rows={counted:<8} "
          f"failures={len(entry.get('failures', {})):<5} queued={queued}")
PY
}

start() {
  cd "${ROOT_DIR}"
  if [ ! -f "${BASE}/data/production/registry-state.json" ]; then
    echo "no state at ${BASE}; seeding from origin/artifact-data first"
    seed
  fi
  docker build --file Dockerfile.crawl --tag "${IMAGE}" . >/dev/null
  if [[ " ${SOURCES} " == *" go "* ]]; then
    tools/go_image.sh runtime "${GO_IMAGE}" >/dev/null
  fi
  for source in ${SOURCES}; do
    local dir="${BASE}-${source}"
    mkdir -p "${dir}/data/production/intermediate" "${dir}/reports"
    # Split this source's slice out of the combined state rather than re-seeding it.
    python3 - "$dir" "$source" "$BASE" <<'PY'
import json, pathlib, sys
directory, source = pathlib.Path(sys.argv[1]), sys.argv[2]
target = directory / "data/production/registry-state.json"
if target.is_file():
    raise SystemExit(0)
# Read the base this run was told to use; hardcoding the default silently ignored BASE.
combined = pathlib.Path(sys.argv[3]) / "data/production/registry-state.json"
state = json.loads(combined.read_text()) if combined.is_file() else {"version": 1, "sources": {}}
slice_ = {"version": 1, "sources": {source: state.get("sources", {}).get(source, {})}}
target.write_text(json.dumps(slice_, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
PY
    for name in $(catalog_for "${source}"); do
      if [ -f "${BASE}/data/production/${name}" ] && [ ! -f "${dir}/data/production/${name}" ]; then
        cp "${BASE}/data/production/${name}" "${dir}/data/production/${name}"
      fi
    done
    if [ -f "${BASE}/data/production/intermediate/${source}.jsonl" ] && \
       [ ! -f "${dir}/data/production/intermediate/${source}.jsonl" ]; then
      cp "${BASE}/data/production/intermediate/${source}.jsonl" "${dir}/data/production/intermediate/${source}.jsonl"
    fi
    docker rm -f "ge-${source}" >/dev/null 2>&1 || true
    if [ "${source}" = go ]; then
      docker run --detach --init --name "ge-${source}" --restart on-failure:5 \
        -v "${dir}:/state" "${GO_IMAGE}" crawl --passes 0 \
        --package-budget "${PACKAGE_BUDGET}" --byte-budget "${BYTE_BUDGET}" >/dev/null
    else
      docker run --detach --rm --init --name "ge-${source}" -v "${dir}:/state" \
        -e "SOURCES=${source}" -e "PACKAGE_BUDGET=${PACKAGE_BUDGET}" \
        -e "BYTE_BUDGET=${BYTE_BUDGET}" -e "PASSES=0" "${IMAGE}" >/dev/null
    fi
    echo "started ge-${source} against ${dir}"
  done
}

status() {
  for source in ${SOURCES}; do
    local dir="${BASE}-${source}"
    printf '%-10s %-24s ' "${source}" "$(docker ps --filter "name=ge-${source}" --format '{{.Status}}' || echo 'not running')"
    python3 - "$dir" "$source" <<'PY'
import json, pathlib, sys
state = pathlib.Path(sys.argv[1]) / "data/production/registry-state.json"
source = json.loads(state.read_text()).get("sources", {}).get(sys.argv[2], {}) if state.is_file() else {}
size, cursor = source.get("catalog_size"), source.get("cursor")
print(f"{cursor:,}/{size:,}" if isinstance(size, int) and isinstance(cursor, int) else f"cursor={cursor}")
PY
  done
}

merge() {
  local out="${BASE}"
  mkdir -p "${out}/data/production/intermediate"
  python3 - "$BASE" "$SOURCES" <<'PY'
import json, pathlib, sys
base, sources = pathlib.Path(sys.argv[1]), sys.argv[2].split()
target = base / "data/production/registry-state.json"
combined = json.loads(target.read_text()) if target.is_file() else {"version": 1, "sources": {}}
for source in sources:
    slice_ = base.parent / f"{base.name}-{source}/data/production/registry-state.json"
    if slice_.is_file():
        combined.setdefault("sources", {}).update(json.loads(slice_.read_text()).get("sources", {}))
target.write_text(json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print(f"merged {len(sources)} source states into {target}")
PY
  for source in ${SOURCES}; do
    local dir="${BASE}-${source}"
    for name in $(catalog_for "${source}"); do
      [ -f "${dir}/data/production/${name}" ] && cp "${dir}/data/production/${name}" "${out}/data/production/${name}"
    done
    if [ -f "${dir}/data/production/intermediate/${source}.jsonl" ]; then
      cp "${dir}/data/production/intermediate/${source}.jsonl" "${out}/data/production/intermediate/${source}.jsonl"
      printf '  %-10s %s rows\n' "${source}" "$(wc -l < "${dir}/data/production/intermediate/${source}.jsonl" | tr -d ' ')"
    fi
  done
}

# Publish only the sources this machine owns.  CI writes crates.io to the same branch,
# so replacing the whole state would overwrite whichever writer published second — the
# mistake that nearly rolled Go's catalog back fifteen months.
publish() {
  local worktree=/tmp/ge-artifact-publish
  local attempt="${PUBLISH_ATTEMPT:-1}"
  local push_status=0
  cd "${ROOT_DIR}"
  git fetch origin artifact-data --quiet
  git worktree remove "${worktree}" --force >/dev/null 2>&1 || true
  rm -rf "${worktree}"
  git worktree prune
  git worktree add --quiet "${worktree}" origin/artifact-data
  mkdir -p "${worktree}/data/production/intermediate" "${worktree}/reports"
  for source in ${SOURCES}; do
    local dir="${BASE}-${source}"
    local state="${dir}/data/production/registry-state.json"
    [ -f "${state}" ] || continue
    if python3 "${ROOT_DIR}/tools/merge_registry_publication.py" \
        --source "${source}" \
        --published-state "${worktree}/data/production/registry-state.json" \
        --local-state "${state}" \
        --published-report "${worktree}/reports/registry-artifact-crawl.json" \
        --local-report "${dir}/reports/registry-artifact-crawl.json"; then
      :
    else
      local merge_status=$?
      if [ "${merge_status}" -eq 3 ]; then
        continue
      fi
      return "${merge_status}"
    fi
    for name in $(catalog_for "${source}"); do
      [ -f "${dir}/data/production/${name}" ] && cp "${dir}/data/production/${name}" "${worktree}/data/production/${name}"
    done
    [ -f "${dir}/data/production/intermediate/${source}.jsonl" ] && \
      cp "${dir}/data/production/intermediate/${source}.jsonl" "${worktree}/data/production/intermediate/${source}.jsonl"
  done
  for source in ${OBSERVATION_SOURCES}; do
    local rows="data/production/intermediate/${source}.jsonl"
    local observed="${ROOT_DIR}/${rows}"
    local target="${worktree}/${rows}"
    if [ -f "${observed}" ]; then
      # Moving package indexes are evidence over time.  Merge by provider identity so
      # a newly observed version wins without deleting a command that disappeared.
      python3 - "${observed}" "${target}" <<'PYOBS'
import json, pathlib, sys
observed, target = map(pathlib.Path, sys.argv[1:])

def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []

def identity(row):
    return row.get("command"), row.get("ecosystem"), row.get("package"), row.get("source")

merged = {identity(row): row for row in rows(observed)}
for row in rows(target):
    merged.setdefault(identity(row), row)
ordered = sorted(merged.values(), key=lambda row: (row.get("command", ""), row.get("package", ""), row.get("source", "")))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ordered))
PYOBS
    fi
  done
  git -C "${worktree}" add -f data/production
  if [ -f "${worktree}/reports/registry-artifact-crawl.json" ]; then
    git -C "${worktree}" add -f reports/registry-artifact-crawl.json
  fi
  if git -C "${worktree}" diff --cached --quiet; then
    echo "nothing to publish"
  else
    local message="Record crawl data"
    [ -n "${SOURCES}" ] && message="${message}; registries: ${SOURCES}"
    [ -n "${OBSERVATION_SOURCES}" ] && message="${message}; observations: ${OBSERVATION_SOURCES}"
    git -C "${worktree}" commit --quiet -m "${message}"
    if git -C "${worktree}" push --quiet origin HEAD:artifact-data; then
      echo "published"
    else
      push_status=$?
    fi
  fi
  git worktree remove "${worktree}" --force || true
  if [ "${push_status}" -ne 0 ]; then
    if [ "${attempt}" -ge "${PUBLISH_MAX_ATTEMPTS}" ]; then
      echo "artifact-data publish failed after ${attempt} attempts" >&2
      return "${push_status}"
    fi
    echo "artifact-data advanced concurrently; rebuilding from the latest branch (attempt $((attempt + 1))/${PUBLISH_MAX_ATTEMPTS})" >&2
    sleep "$((attempt * 5))"
    PUBLISH_ATTEMPT=$((attempt + 1)) publish
  fi
}

# The manual command and the long-running supervisor share BASE and therefore the
# same temporary worktree.  Serialise the entire publication, not just the push: two
# writers racing in worktree cleanup can remove the checkout while the other is
# preparing its commit.  A skipped manual run is safe because the active publisher
# already owns the same local source snapshots.
publish_locked() (
  if ! flock -n 9; then
    echo "publication already in progress for ${BASE}; skipping"
    return 0
  fi
  publish
) 9>"${BASE}.publish.lock"

# Publishing by hand means the crawl's progress lives only on this machine until
# someone remembers.  `watch` keeps publishing while the containers run, so stopping
# the session strands nothing.
watch() {
  local interval="${PUBLISH_INTERVAL:-1800}"
  while :; do
    sleep "${interval}"
    if ! docker ps --format '{{.Names}}' | grep -q '^ge-'; then
      echo "$(date -u +%FT%TZ) no crawl containers left; stopping the publisher"
      break
    fi
    printf '%s ' "$(date -u +%FT%TZ)"
    publish_locked 2>&1 | grep -vE '^remote:' | tr '\n' ' '
    echo
  done
}

case "${1:-start}" in
  start) start ;;
  seed) seed ;;
  status) status ;;
  merge) merge ;;
  publish) publish_locked ;;
  watch) watch ;;
  stop) for source in ${SOURCES}; do docker stop -t 120 "ge-${source}" >/dev/null 2>&1 && echo "stopped ge-${source}"; done ;;
  *) echo "usage: $0 {seed|start|status|merge|publish|watch|stop}" >&2; exit 2 ;;
esac
