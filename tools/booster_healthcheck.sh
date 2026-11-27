#!/usr/bin/env bash
# Container health of the booster (Dockerfile.booster): healthy while the supervisor is alive
# and the crawler has committed a package within HEALTH_MAX_STALE seconds (default 3600, more
# than the supervisor's own STALL_TIMEOUT so its restart gets the first chance). A missing
# progress file means the crawler has not finished its first package yet (seeding, opening
# the store); the HEALTHCHECK start period covers that.
set -u
SOURCE="${BOOSTER_SOURCE:-pypi}"
DIR="${BASE:-${HOME}/.ge-crawl}-${SOURCE}"
MAX_STALE="${HEALTH_MAX_STALE:-3600}"

pid="$(cat "${DIR}/supervisor.pid" 2>/dev/null || true)"
if [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null; then
  echo "no live supervisor for ${DIR}"
  exit 1
fi
[ -f "${DIR}/progress" ] || { echo "supervisor ${pid}; no progress yet"; exit 0; }
age=$(( $(date +%s) - $(stat -c %Y "${DIR}/progress" 2>/dev/null || stat -f %m "${DIR}/progress") ))
if [ "${age}" -gt "${MAX_STALE}" ]; then
  echo "no crawler progress for ${age}s"
  exit 1
fi
echo "supervisor ${pid}; progress ${age}s ago"
