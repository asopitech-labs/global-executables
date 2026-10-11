#!/usr/bin/env bash
# Crawl one registry's backlog from your own machine, in parallel with the Actions refresh.
#
#   CONTACT=you@example.org tools/local_booster.sh pypi run
#
# What it does (docs/OPERATIONS.md "Local booster"):
#   1. leases buckets of the catalogue on artifact-data, so the Actions refresh leaves their
#      rotation to you while the lease heartbeat is alive (a stopped machine simply expires);
#   2. seeds a local working directory from artifact-data (first run only) and starts the Go
#      crawler on those buckets only, without the change feed, politely paced;
#   3. every PUBLISH_INTERVAL seconds publishes what it found as a *delta* (only the packages
#      you changed) and renews the lease; on a signal it stops, publishes once more and
#      releases the lease.
# The schedule cache lives with the working directory and survives restarts.
#
# Commands: run | status | publish | stop | release | unit   (default: run)
# Settings (environment):
#   CONTACT             required: an e-mail address or URL put in the User-Agent
#   BOOSTER_ID          lease name, default: this machine's host name
#   BOOSTER_RANGES      buckets to own, default 0-255 (everything); BOOSTER_SLICE=i/n takes the
#                       i-th of n equal slices instead (extra machines: 0/2 and 1/2)
#   WORKERS             concurrent inspections, default 8
#   MAX_BYTES_PER_SECOND  download cap, default 5000000 (5 MB/s); 0 = none
#   PUBLISH_INTERVAL    seconds between publications, default 3600
#   PAUSE               seconds between passes, default 30 (+-30 % jitter)
#   PACKAGE_BUDGET      packages per pass, default 3000
#   LEASE_TTL_HOURS     how long a heartbeat keeps the lease alive, default 12
#   HEARTBEAT_INTERVAL  seconds between lease heartbeats, default 3600; a heartbeat commits only
#                       once the last one is older than LEASE_MIN_AGE hours (default TTL/3)
#   STOP_TIMEOUT        seconds to wait for the crawler to exit on stop, default 300
#   CRAWLER_BIN         native: use this crawler binary instead of building one
#   BASE                working directory prefix, default ~/.ge-crawl
#   MODE                docker (default; or podman) or native (needs Go; no container)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-pypi}"
COMMAND="${2:-run}"
case "${SOURCE}" in pypi|rubygems|packagist) ;; *) echo "usage: $0 {pypi|rubygems|packagist} [run|status|publish|stop|release|unit]" >&2; exit 2 ;; esac

BASE="${BASE:-${HOME}/.ge-crawl}"
BOOSTER_ID="${BOOSTER_ID:-$(hostname -s 2>/dev/null || hostname)}"
if [ -n "${BOOSTER_SLICE:-}" ]; then
  BOOSTER_RANGES="$(PYTHONPATH="${ROOT_DIR}/src" python3 -c "
import sys
from global_executables.booster import slice_ranges
index, _, count = sys.argv[1].partition('/')
print(slice_ranges(int(index), int(count)))" "${BOOSTER_SLICE}")"
fi
BOOSTER_RANGES="${BOOSTER_RANGES:-0-255}"
WORKERS="${WORKERS:-8}"
MAX_BYTES_PER_SECOND="${MAX_BYTES_PER_SECOND:-5000000}"
PUBLISH_INTERVAL="${PUBLISH_INTERVAL:-3600}"
PAUSE="${PAUSE:-30}"
PACKAGE_BUDGET="${PACKAGE_BUDGET:-3000}"
MODE="${MODE:-docker}"
LEASE_TTL_HOURS="${LEASE_TTL_HOURS:-12}"
LEASE_MIN_AGE="${LEASE_MIN_AGE:-$(python3 -c "print(${LEASE_TTL_HOURS} / 3)")}"
DIR="${BASE}-${SOURCE}"
NATIVE_PID="${DIR}/booster.pid"
SUPERVISOR_PID="${DIR}/supervisor.pid"
STOP_TIMEOUT="${STOP_TIMEOUT:-300}"
# Heartbeat: the lease is renewed every HEARTBEAT_INTERVAL seconds, but a renewal only commits
# (one small file, no data) once the heartbeat is older than LEASE_MIN_AGE hours (default a
# third of the TTL), so the TTL does not have to follow PUBLISH_INTERVAL.
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-3600}"

export BASE BOOSTER=1 SOURCES="${SOURCE}" OBSERVATION_SOURCES=" " DELTA_SOURCES="${DELTA_SOURCES:-${SOURCE}}"
export PACKAGE_BUDGET PUBLISH_LOCK="${PUBLISH_LOCK:-/tmp/global-executables-artifact-publish.lock}"

say() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
lease() { python3 "${ROOT_DIR}/tools/booster.py" "$1" --source "${SOURCE}" --id "${BOOSTER_ID}" --ranges "${BOOSTER_RANGES}" --ttl-hours "${LEASE_TTL_HOURS}" "${@:2}"; }

user_agent() {
  [ -n "${CONTACT:-}" ] || { echo "set CONTACT to an e-mail address or URL: it goes into the User-Agent so the registry can reach you" >&2; exit 2; }
  echo "global-executables-booster/1 (+https://github.com/asopitech-labs/global-executables; ${CONTACT})"
}

crawler_args() {
  echo "--continuous --rotation-include ${BOOSTER_RANGES} --no-feed --workers ${WORKERS} \
--max-bytes-per-second ${MAX_BYTES_PER_SECOND} --pause ${PAUSE}s --pause-jitter 0.3"
}

# The native crawler's PID. It is the PID of the crawler process itself (not of a subshell
# that waits for it): the file is written by the shell that started it with `$!` of a
# simple background command, so TERM reaches the crawler and `kill -0` tells the truth.
# A stale file (the process died, or the PID was reused by something else) is ignored and
# removed: only a live process whose command line names the crawler counts.
crawler_pid() {
  local pid
  if [ -f "${NATIVE_PID}" ]; then
    pid="$(cat "${NATIVE_PID}" 2>/dev/null || true)"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null && ps -p "${pid}" -o command= 2>/dev/null | grep -q 'crawl --source'; then
      echo "${pid}"
      return 0
    fi
    rm -f "${NATIVE_PID}"
  fi
  return 1
}

# Any crawler for this source working in this directory, with or without a PID file (one
# started by an older version of this script left none).
orphan_pids() {
  pgrep -f "crawl --source ${SOURCE} .*--rotation-include" 2>/dev/null | while read -r pid; do
    [ "$(readlink "/proc/${pid}/cwd" 2>/dev/null || true)" = "${DIR}" ] && echo "${pid}"
  done
  return 0
}

running() {
  if [ "${MODE}" = native ]; then
    crawler_pid >/dev/null
  else
    local runtime="${CONTAINER_RUNTIME:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}"
    "${runtime}" ps --format '{{.Names}}' | grep -qx "ge-${SOURCE}"
  fi
}

refuse_double_start() {
  if running || [ -n "$(orphan_pids)" ]; then
    echo "a ${SOURCE} booster crawler is already running for ${DIR}; stop it first (tools/local_booster.sh ${SOURCE} stop)" >&2
    exit 3
  fi
  if [ -f "${SUPERVISOR_PID}" ]; then
    local other
    other="$(cat "${SUPERVISOR_PID}" 2>/dev/null || true)"
    if [ -n "${other}" ] && [ "${other}" != "$$" ] && kill -0 "${other}" 2>/dev/null \
       && ps -p "${other}" -o command= 2>/dev/null | grep -q 'local_booster'; then
      echo "another booster supervisor (pid ${other}) is already running for ${DIR}" >&2
      exit 3
    fi
  fi
}

start_crawler() {
  if [ "${MODE}" = native ]; then
    NO_CONTAINER=1 bash "${ROOT_DIR}/tools/crawl_parallel.sh" start
    local binary="${CRAWLER_BIN:-${BASE}-bin/go-registry-crawler}"
    if [ -z "${CRAWLER_BIN:-}" ]; then
      mkdir -p "$(dirname "${binary}")"
      (cd "${ROOT_DIR}" && go build -o "${binary}" ./cmd/go-registry-crawler)
    fi
    cd "${DIR}"
    # A simple background command, so $! is the crawler (nohup execs it) and not a subshell.
    # shellcheck disable=SC2046,SC2086
    nohup "${binary}" crawl --source "${SOURCE}" --passes 0 --package-budget "${PACKAGE_BUDGET}" \
      $(crawler_args) >> "${DIR}/booster.log" 2>&1 &
    echo $! > "${NATIVE_PID}"
    cd "${ROOT_DIR}"
  else
    CRAWL_EXTRA_ARGS="$(crawler_args)" bash "${ROOT_DIR}/tools/crawl_parallel.sh" start
  fi
}

# Stops the crawler and returns only when it has exited (TERM first: it cancels the pass and
# saves; KILL only after STOP_TIMEOUT seconds). Returns non-zero if it is still alive.
stop_crawler() {
  if [ "${MODE}" = native ]; then
    local pids pid waited=0
    pids="$(crawler_pid || true) $(orphan_pids)"
    for pid in ${pids}; do kill -TERM "${pid}" 2>/dev/null || true; done
    while [ -n "$(echo "$(crawler_pid || true) $(orphan_pids)" | tr -d ' ')" ] && [ "${waited}" -lt "${STOP_TIMEOUT}" ]; do
      sleep 1
      waited=$((waited + 1))
    done
    for pid in $(crawler_pid || true) $(orphan_pids); do
      say "crawler ${pid} ignored TERM for ${STOP_TIMEOUT}s; killing it"
      kill -KILL "${pid}" 2>/dev/null || true
      sleep 1
    done
    if [ -n "$(echo "$(crawler_pid || true) $(orphan_pids)" | tr -d ' ')" ]; then
      echo "the crawler is still alive; not publishing over it" >&2
      return 1
    fi
    [ -z "${pids// /}" ] || say "native crawler stopped"
    rm -f "${NATIVE_PID}"
  else
    bash "${ROOT_DIR}/tools/crawl_parallel.sh" stop || true
  fi
}

publish_now() {
  if [ -n "${PUBLISH_CMD:-}" ]; then
    bash -c "${PUBLISH_CMD}" || true
  elif command -v flock >/dev/null 2>&1; then
    bash "${ROOT_DIR}/tools/crawl_parallel.sh" publish 2>&1 | grep -vE '^remote:' | tr '\n' ' ' || true
    echo
  else
    echo "flock is missing (macOS: brew install flock); publishing without the machine-wide lock" >&2
    bash "${ROOT_DIR}/tools/crawl_parallel.sh" publish_unlocked 2>&1 | grep -vE '^remote:' | tr '\n' ' ' || true
    echo
  fi
}

progress_json() {
  python3 - "${ROOT_DIR}" "${DIR}" "${SOURCE}" <<'PY'
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "src"))
from global_executables.refresh_policy import read_cache
cache = pathlib.Path(sys.argv[2]) / "data/production/cache" / f"{sys.argv[3]}.cache.gz"
checks, cursor, found = read_cache(cache)
print(json.dumps({"checked": len(checks), "cache": found}))
PY
}

case "${COMMAND}" in
  run)
    GE_USER_AGENT="$(user_agent)"
    export GE_USER_AGENT
    git -C "${ROOT_DIR}" ls-remote --exit-code origin artifact-data >/dev/null \
      || { echo "cannot reach origin/artifact-data with your git credentials" >&2; exit 1; }
    refuse_double_start
    say "leasing buckets ${BOOSTER_RANGES} of ${SOURCE} as ${BOOSTER_ID}"
    lease acquire || { echo "the lease was refused: another live booster holds part of ${BOOSTER_RANGES}" >&2; exit 4; }
    echo "$$" > "${SUPERVISOR_PID}"
    finish() {
      trap - INT TERM EXIT
      say "stopping: crawler, one last publication, lease release"
      # Order matters: the crawler must have exited before the final publication, or the
      # publication races with a crawler that is still writing its state.
      if stop_crawler; then
        publish_now
        lease release || true
      else
        say "crawler still alive: no final publication, the lease stays until it expires"
      fi
      rm -f "${SUPERVISOR_PID}"
    }
    trap finish INT TERM EXIT
    start_crawler
    tick="${HEARTBEAT_INTERVAL}"
    [ "${tick}" -gt "${PUBLISH_INTERVAL}" ] && tick="${PUBLISH_INTERVAL}"
    last_publish="$(date +%s)"
    while :; do
      sleep "${tick}" &
      wait $! || true
      if ! running; then say "the crawler is not running any more"; break; fi
      if [ "$(( $(date +%s) - last_publish ))" -ge "${PUBLISH_INTERVAL}" ]; then
        printf '%s ' "$(date -u +%FT%TZ)"
        publish_now
        last_publish="$(date +%s)"
        lease renew --progress "$(progress_json)" >/dev/null || say "lease renewal failed; will retry"
      else
        # Heartbeat only: commits nothing unless the lease is older than LEASE_MIN_AGE.
        lease renew --min-age-hours "${LEASE_MIN_AGE}" >/dev/null || say "lease heartbeat failed; will retry"
      fi
    done
    ;;
  status)
    running && echo "crawler: running" || echo "crawler: not running"
    python3 "${ROOT_DIR}/tools/booster.py" show --source "${SOURCE}" || true
    progress_json || true
    ;;
  publish) publish_now ;;
  stop) stop_crawler; [ -z "${SUPERVISOR_STOP:-}" ] || true ;;
  release) lease release ;;
  unit)
    cat <<UNIT
# --- systemd (Linux, WSL2 with systemd): ~/.config/systemd/user/ge-booster-${SOURCE}.service
[Unit]
Description=global-executables ${SOURCE} booster
After=network-online.target
[Service]
Environment=CONTACT=${CONTACT:-you@example.org} BOOSTER_ID=${BOOSTER_ID}
ExecStart=${ROOT_DIR}/tools/local_booster.sh ${SOURCE} run
Restart=on-failure
RestartSec=300
[Install]
WantedBy=default.target
#   systemctl --user daemon-reload && systemctl --user enable --now ge-booster-${SOURCE}
#   loginctl enable-linger \$USER      # keep it running when you are logged out

# --- launchd (macOS): ~/Library/LaunchAgents/org.global-executables.booster.${SOURCE}.plist
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>org.global-executables.booster.${SOURCE}</string>
  <key>ProgramArguments</key><array><string>${ROOT_DIR}/tools/local_booster.sh</string><string>${SOURCE}</string><string>run</string></array>
  <key>EnvironmentVariables</key><dict><key>CONTACT</key><string>${CONTACT:-you@example.org}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${HOME}/.ge-crawl-${SOURCE}.log</string><key>StandardErrorPath</key><string>${HOME}/.ge-crawl-${SOURCE}.log</string>
</dict></plist>
#   launchctl load ~/Library/LaunchAgents/org.global-executables.booster.${SOURCE}.plist

# --- cron (any): start it at boot and let the lease heartbeat tell Actions it is alive.
#   @reboot CONTACT=${CONTACT:-you@example.org} ${ROOT_DIR}/tools/local_booster.sh ${SOURCE} run >> \$HOME/.ge-crawl-${SOURCE}.log 2>&1
UNIT
    ;;
  *) echo "usage: $0 ${SOURCE} {run|status|publish|stop|release|unit}" >&2; exit 2 ;;
esac
