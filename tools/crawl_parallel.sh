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
BASE="${BASE:-${HOME}/.ge-crawl}"
SOURCES="${SOURCES:-pypi rubygems packagist nuget npm go}"
PACKAGE_BUDGET="${PACKAGE_BUDGET:-3000}"
BYTE_BUDGET="${BYTE_BUDGET:-8000000000}"

# Catalogs are per source, so each container carries only the one it reads.
catalog_for() {
  case "$1" in
    pypi) echo "pypi-projects.txt" ;;
    rubygems) echo "rubygems-names.txt" ;;
    packagist) echo "packagist-packages.txt" ;;
    nuget) echo "nuget-tools.txt" ;;
    npm) echo "npm-packages.txt" ;;
    go) echo "go-modules.txt go-modules.cursor.json" ;;
    *) echo "" ;;
  esac
}

start() {
  cd "${ROOT_DIR}"
  docker build --file Dockerfile.crawl --tag "${IMAGE}" . >/dev/null
  for source in ${SOURCES}; do
    local dir="${BASE}-${source}"
    mkdir -p "${dir}/data/production/intermediate" "${dir}/reports"
    # Split this source's slice out of the combined state rather than re-seeding it.
    python3 - "$dir" "$source" <<'PY'
import json, pathlib, sys
directory, source = pathlib.Path(sys.argv[1]), sys.argv[2]
target = directory / "data/production/registry-state.json"
if target.is_file():
    raise SystemExit(0)
combined = pathlib.Path.home() / ".ge-crawl/data/production/registry-state.json"
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
    docker run --detach --rm --init --name "ge-${source}" -v "${dir}:/state" \
      -e "SOURCES=${source}" -e "PACKAGE_BUDGET=${PACKAGE_BUDGET}" \
      -e "BYTE_BUDGET=${BYTE_BUDGET}" -e "PASSES=0" "${IMAGE}" >/dev/null
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

case "${1:-start}" in
  start) start ;;
  status) status ;;
  merge) merge ;;
  stop) for source in ${SOURCES}; do docker stop -t 120 "ge-${source}" >/dev/null 2>&1 && echo "stopped ge-${source}"; done ;;
  *) echo "usage: $0 {start|status|merge|stop}" >&2; exit 2 ;;
esac
