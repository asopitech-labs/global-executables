#!/usr/bin/env bash
# Build a named Go crawler image stage with Docker/Podman-equivalent context rules.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-runtime}"
TAG="${2:-global-executables-go-crawler:local}"

if [ -n "${CONTAINER_RUNTIME:-}" ]; then
  RUNTIME="${CONTAINER_RUNTIME}"
elif command -v podman >/dev/null 2>&1; then
  RUNTIME=podman
elif command -v docker >/dev/null 2>&1; then
  RUNTIME=docker
else
  echo "Podman or Docker is required." >&2
  exit 1
fi

IGNORE_ARGS=()
if [ "$(basename "${RUNTIME}")" = podman ]; then
  # Docker discovers Dockerfile.go-crawler.dockerignore automatically. Podman
  # supports the same syntax through an explicit --ignorefile option.
  IGNORE_ARGS=(--ignorefile "${ROOT_DIR}/Dockerfile.go-crawler.dockerignore")
fi

exec "${RUNTIME}" build \
  "${IGNORE_ARGS[@]}" \
  --file "${ROOT_DIR}/Dockerfile.go-crawler" \
  --target "${TARGET}" \
  --tag "${TAG}" \
  "${ROOT_DIR}"
