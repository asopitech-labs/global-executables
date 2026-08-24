#!/usr/bin/env bash
# Shared local/CI validation pipeline for the Go crawler.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

environment() {
  local expected actual selection
  expected="$(awk '$1 == "toolchain" { print $2 }' go.mod)"
  actual="$(go env GOVERSION)"
  selection="$(go env GOTOOLCHAIN)"
  if [ -z "${expected}" ] || [ "${actual}" != "${expected}" ]; then
    echo "Go toolchain mismatch: go.mod=${expected:-missing}, running=${actual}" >&2
    return 1
  fi
  if [ "${selection}" != local ]; then
    echo "GOTOOLCHAIN must be local, got ${selection}" >&2
    return 1
  fi
  printf 'toolchain: %s (%s/%s)\n' "${actual}" "$(go env GOOS)" "$(go env GOARCH)"
}

dependencies() {
  go mod tidy -diff
  go mod verify
}

formatting() {
  local files=() unformatted
  while IFS= read -r -d '' file; do
    files+=("${file}")
  done < <(find cmd internal -type f -name '*.go' -print0 2>/dev/null)
  if [ "${#files[@]}" -eq 0 ]; then
    echo "no Go source files found" >&2
    return 1
  fi
  unformatted="$(gofmt -l "${files[@]}")"
  if [ -n "${unformatted}" ]; then
    printf 'gofmt required:\n%s\n' "${unformatted}" >&2
    return 1
  fi
}

tests() {
  go vet ./...
  go test ./...
  CGO_ENABLED=1 go test -race ./...
}

security() {
  go tool govulncheck ./...
}

build() {
  local output_dir="${GO_OUTPUT_DIR:-dist}"
  mkdir -p "${output_dir}"
  CGO_ENABLED=0 go build -trimpath -buildvcs=false \
    -o "${output_dir}/go-registry-crawler" ./cmd/go-registry-crawler
  "${output_dir}/go-registry-crawler" --version
}

check() {
  environment
  dependencies
  formatting
  tests
  security
  build
}

case "${1:-check}" in
  environment) environment ;;
  dependencies) environment; dependencies ;;
  format) environment; formatting ;;
  test) environment; dependencies; formatting; tests ;;
  security) environment; security ;;
  build) environment; build ;;
  check) check ;;
  *) echo "usage: $0 {environment|dependencies|format|test|security|build|check}" >&2; exit 2 ;;
esac
