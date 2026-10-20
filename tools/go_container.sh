#!/usr/bin/env bash
# Run a command in the exact digest-pinned Go development environment.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_IMAGE="${GO_DEV_IMAGE:-global-executables-go-development:local}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 COMMAND [ARG ...]" >&2
  exit 2
fi

if [ -n "${CONTAINER_RUNTIME:-}" ]; then
  RUNTIME="${CONTAINER_RUNTIME}"
elif command -v podman >/dev/null 2>&1; then
  RUNTIME=podman
elif command -v docker >/dev/null 2>&1; then
  RUNTIME=docker
else
  echo "Podman or Docker is required; a host Go installation is not required." >&2
  exit 1
fi

BUILD_IGNORE_ARGS=()
if [ "$(basename "${RUNTIME}")" = podman ]; then
  BUILD_IGNORE_ARGS=(--ignorefile "${ROOT_DIR}/Dockerfile.go-crawler.dockerignore")
fi

CACHE_DIR="${GO_CONTAINER_CACHE:-${ROOT_DIR}/.cache/go}"
mkdir -p "${CACHE_DIR}/build" "${CACHE_DIR}/mod" "${CACHE_DIR}/path"

"${RUNTIME}" build --quiet \
  "${BUILD_IGNORE_ARGS[@]}" \
  --file "${ROOT_DIR}/Dockerfile.go-crawler" \
  --target go-development \
  --tag "${DEV_IMAGE}" \
  "${ROOT_DIR}" >/dev/null

USER_ARGS=(--user "$(id -u):$(id -g)")
if [ "$(basename "${RUNTIME}")" = podman ]; then
  USER_ARGS=(--userns=keep-id "${USER_ARGS[@]}")
fi

exec "${RUNTIME}" run --rm --init \
  "${USER_ARGS[@]}" \
  --env GOTOOLCHAIN=local \
  --env HOME=/tmp \
  --env GOCACHE=/go-cache/build \
  --env GOMODCACHE=/go-cache/mod \
  --env GOPATH=/go-cache/path \
  --volume "${ROOT_DIR}:/workspace" \
  --volume "${CACHE_DIR}:/go-cache" \
  --workdir /workspace \
  "${DEV_IMAGE}" "$@"
