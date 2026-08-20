#!/bin/sh
# Run the bounded registry crawl back to back until every selected source is exhaustive.
#
# Each pass is budgeted, so the resumable state is checkpointed between passes and
# stopping the container loses at most the pass in flight.  Paths are relative to the
# mounted working directory, which mirrors the artifact-data branch layout.
set -eu

SOURCES=${SOURCES:-"npm pypi crates go rubygems packagist"}
PACKAGE_BUDGET=${PACKAGE_BUDGET:-2000}
BYTE_BUDGET=${BYTE_BUDGET:-5000000000}
SOURCE_PACKAGE_BUDGETS=${SOURCE_PACKAGE_BUDGETS:-}
PASSES=${PASSES:-0}
PAUSE_SECONDS=${PAUSE_SECONDS:-5}
TIMEOUT=${TIMEOUT:-300}

mkdir -p data/production/intermediate reports

set --
for source in ${SOURCES}; do set -- "$@" --source "${source}"; done
for override in ${SOURCE_PACKAGE_BUDGETS}; do set -- "$@" --source-package-budget "${override}"; done

summarise() {
  python - <<'PY'
import json, pathlib
report = json.loads(pathlib.Path("reports/registry-artifact-crawl.json").read_text())
for name, source in sorted(report.get("sources", {}).items()):
    size = source.get("catalog_size")
    position = f"{source.get('cursor', 0):,}/{size:,}" if size else source.get("coverage_kind", "?")
    note = source.get("error", "")
    print(f"  {name:10} {str(source.get('coverage_kind')):10} {position:>20}"
          f"  records={source.get('records', 0):<7} failures={source.get('failures', 0):<5}{note}")
PY
}

processed_nothing() {
  python - <<'PYIDLE'
import json, pathlib, sys
report = json.loads(pathlib.Path("reports/registry-artifact-crawl.json").read_text())
sources = report.get("sources", {}).values()
sys.exit(0 if sources and all((s.get("processed") or 0) == 0 for s in sources) else 1)
PYIDLE
}

exhaustive() {
  python - <<'PY'
import json, pathlib, sys
report = json.loads(pathlib.Path("reports/registry-artifact-crawl.json").read_text())
sys.exit(0 if report.get("coverage_kind") == "exhaustive" else 1)
PY
}

pass=0
idle=0
while :; do
  pass=$((pass + 1))
  printf '=== pass %s  %s ===\n' "${pass}" "$(date -u +%FT%TZ)"
  # A failed pass still publishes its progress, so keep going rather than unwinding.
  python /app/tools/registry_artifact_crawl.py "$@" \
    --state data/production/registry-state.json \
    --output-dir data/production/intermediate \
    --report reports/registry-artifact-crawl.json \
    --package-budget "${PACKAGE_BUDGET}" \
    --byte-budget "${BYTE_BUDGET}" \
    --timeout "${TIMEOUT}" >/dev/null || true

  if [ -f reports/registry-artifact-crawl.json ]; then
    summarise
  else
    echo "  no report produced"
    exit 1
  fi

  if exhaustive; then
    echo "=== every selected source is exhaustive ==="
    break
  fi
  # A source whose cursor has reached the end of its catalog returns instantly having
  # processed nothing.  Retrying that every few seconds is a busy loop, so back off and
  # stop once it is clear no further pass can achieve anything.
  if processed_nothing; then
    idle=$((idle + 1))
    if [ "${idle}" -ge "${IDLE_PASSES:-5}" ]; then
      printf '=== no pass processed anything in %s attempts; stopping ===\n' "${idle}"
      break
    fi
    sleep $((PAUSE_SECONDS * idle * 6))
    continue
  fi
  idle=0
  if [ "${PASSES}" -ne 0 ] && [ "${pass}" -ge "${PASSES}" ]; then
    printf '=== stopping after %s passes ===\n' "${pass}"
    break
  fi
  sleep "${PAUSE_SECONDS}"
done
