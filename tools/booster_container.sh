#!/usr/bin/env bash
# Entrypoint of Dockerfile.booster: make /app a git checkout whose object store lives on the
# volume, give git a credential helper that reads the token file, then hand over to the
# booster supervisor (tools/local_booster.sh), which tini signals on `docker stop`.
#
# Required: CONTACT (User-Agent), BOOSTER_ID (lease name; the container's host name changes
# with every re-creation, so it is not a usable default), a readable GITHUB_TOKEN_FILE with
# push access to artifact-data. Nothing here prints the token.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${BOOSTER_SOURCE:-pypi}"
GIT_STORE="${GIT_STORE:-/data/git}"
GITHUB_TOKEN_FILE="${GITHUB_TOKEN_FILE:-/run/secrets/github_token}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/asopitech-labs/global-executables}"

fail() { echo "booster container: $*" >&2; exit 2; }
[ -n "${CONTACT:-}" ] || fail "set CONTACT (an e-mail address or URL for the User-Agent)"
[ -n "${BOOSTER_ID:-}" ] || fail "set BOOSTER_ID (the lease name, e.g. <host>-booster)"
[ -r "${GITHUB_TOKEN_FILE}" ] && [ -s "${GITHUB_TOKEN_FILE}" ] \
  || fail "no readable token at ${GITHUB_TOKEN_FILE} (mount the host's token file there)"

# The object store and the worktree metadata survive re-creation of the container; the
# checkout itself is the image (/app), so code always matches the image.
if [ ! -d "${GIT_STORE}" ]; then
  git init --quiet --separate-git-dir="${GIT_STORE}" "${ROOT_DIR}"
else
  printf 'gitdir: %s\n' "${GIT_STORE}" > "${ROOT_DIR}/.git"
fi
git -C "${ROOT_DIR}" remote remove origin >/dev/null 2>&1 || true
git -C "${ROOT_DIR}" remote add origin "${REPOSITORY_URL}"
# The helper reads the file at use time, so a rotated token needs only a restart at most.
# shellcheck disable=SC2016  # expanded by git's shell, not here
git config --global credential.helper \
  '!f() { [ "$1" = get ] || exit 0; echo username=x-access-token; printf "password=%s\n" "$(cat "$GITHUB_TOKEN_FILE")"; }; f'
git config --global user.name "${GIT_AUTHOR_NAME:-${BOOSTER_ID} booster}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-${CONTACT}}"
export GITHUB_TOKEN_FILE

exec "${ROOT_DIR}/tools/local_booster.sh" "${SOURCE}" "${1:-run}"
