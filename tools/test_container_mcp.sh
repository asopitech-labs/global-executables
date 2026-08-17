#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_IMAGE="global-executables-mcp:container-test"
CODEX_IMAGE="global-executables-codex-mcp-test:container-test"
NETWORK="global-executables-mcp-test"
SERVER_CONTAINER="global-executables-mcp-server-test"
CODEX_VERSION="${CODEX_VERSION:-0.147.0}"

cleanup() {
  docker rm --force "$SERVER_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$ROOT_DIR"

docker rm --force "$SERVER_CONTAINER" >/dev/null 2>&1 || true
docker network rm "$NETWORK" >/dev/null 2>&1 || true

echo "==> Building MCP server image"
docker build --tag "$SERVER_IMAGE" .

echo "==> Building Codex test image"
docker build --build-arg "CODEX_VERSION=$CODEX_VERSION" \
  --file Dockerfile.codex-test \
  --tag "$CODEX_IMAGE" .

docker network create "$NETWORK" >/dev/null

echo "==> Starting MCP server container"
docker run --detach --name "$SERVER_CONTAINER" \
  --network "$NETWORK" --network-alias mcp \
  "$SERVER_IMAGE" >/dev/null

echo "==> Registering MCP in Codex inside the test container"
docker run --rm --network "$NETWORK" "$CODEX_IMAGE" \
  bash -ceu '
    export CODEX_HOME=/root/.codex
    mkdir -p "$CODEX_HOME"
    codex mcp add global-executables --url http://mcp:8000/mcp
    codex mcp list | grep -F "global-executables"
    codex mcp get global-executables | grep -F "http://mcp:8000/mcp"
    python /workspace/tools/container_mcp_probe.py http://mcp:8000/mcp
  '

echo "Container MCP/Codex verification passed."
