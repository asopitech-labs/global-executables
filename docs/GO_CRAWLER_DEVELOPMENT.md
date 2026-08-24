# Go crawler development and build contract

## Purpose

This document defines the development, build, and runtime contract for the
transactional Go registry crawler. The crawler is a durable, network-heavy pipeline,
so a successful build is not just compilation: the same source must pass the same
dependency, concurrency, security, and container checks locally and in CI.

The target is `linux/amd64`, matching the rootless Podman host. Python continues to
own every other registry and remains the Go rollback path through the cutover window.

## At a glance

- Exact compiler: Go 1.26.7 with `GOTOOLCHAIN=local`.
- Development: digest-pinned official container; host Go is optional.
- Validation: one shared format, vet, test, race, vulnerability, and build pipeline.
- Runtime: dedicated scratch image and an explicit `crawl` subcommand.
- Durable state: bbolt commits observations, retries, cursor, and generation together.
- Compatibility: state, JSONL, and report are streamed from one database snapshot.
- Rollback: the Python image can consume the last compatibility export without a DB conversion.

## Quick start

No host Go installation is required. Podman is preferred; Docker is supported.

```console
./tools/go_container.sh ./tools/go_pipeline.sh check
./tools/go_container.sh ./tools/go_pipeline.sh build
./tools/go_image.sh go-test global-executables-go-test:local
./tools/go_image.sh runtime global-executables-go-crawler:local
podman run --rm global-executables-go-crawler:local --version
```

Native Go development is also supported when the installed toolchain exactly
matches the `toolchain` directive in `go.mod`:

```console
GOTOOLCHAIN=local ./tools/go_pipeline.sh check
```

`go.mod` is the canonical Go version and dependency contract. The development
container, CI setup, and pipeline version check must all agree with it.

## Implementation gate

Crawler behavior was allowed only after all of these became true, and these remain
release gates:

- the digest-pinned development image reports the exact `go.mod` toolchain;
- the native and container pipeline entrypoints both pass;
- the Go test stage and final runtime image build in CI;
- the Docker build context excludes repository history and crawl state;
- a bind-mounted write creates files owned by the invoking user;
- the placeholder binary is not routed to production; and
- issue #40 records the environment contract and validation evidence.

The gate passed before crawler behavior was added. Any toolchain or dependency update
must pass it again; Go 1.26.5 was replaced by 1.26.7 when `govulncheck` found reachable
standard-library vulnerabilities, rather than weakening the security gate.

## Environment decision

| Concern | Contract | Reason |
| --- | --- | --- |
| Language | Go, not Java | The workload is dominated by HTTP, ZIP inspection, bounded concurrency, and a static local executable. Go fits the repository and deployment footprint without introducing a JVM. |
| Toolchain | Go 1.26.7, exact patch | This is the maintained Go 1.26 patch release as of 2026-08-25. Patch-level pinning prevents developer and CI drift. |
| Toolchain switching | `GOTOOLCHAIN=local` | A build must fail on a mismatched toolchain instead of silently downloading a different compiler. |
| Local setup | Digest-pinned official Go container | The host currently has no Go installation. Container-first setup makes onboarding reproducible and avoids root-owned repository files. |
| Deployment target | `linux/amd64`, `CGO_ENABLED=0` | This matches the current crawler host and produces a portable static crawler binary. |
| Runtime image | Dedicated minimal Go crawler image | The Go source can be built, rolled back, and operated independently of the Python registry crawlers. |
| Dependencies | Go modules with committed `go.mod` and `go.sum` | Versions and downloaded module contents are reviewable and checksum-verified. |
| Developer tools | Pinned `tool` directives in `go.mod` | Security tools use the same reviewed version locally and in CI. |

The official Go toolchain documentation explains that `go` and `toolchain` in
`go.mod` participate in toolchain selection and that automatic switching may
download a compiler. For this repository, the container and CI install the requested
compiler explicitly, then set `GOTOOLCHAIN=local` to make drift a hard failure.

## Build topology

```text
go.mod + go.sum
       |
       +--> exact Go development image --> local check/build
       |
       +--> setup-go in CI -------------> identical pipeline script
       |
       +--> Dockerfile.go-crawler
              |-- go-development  exact compiler and environment
              |-- go-test         module, vet, test, race gates
              |-- go-build        static, trimpath binary
              `-- runtime         binary + CA certificates only
```

## Runtime topology and recovery

```text
index.golang.org --> atomic append-only catalog --> bounded work coordinator
proxy.golang.org --> module inspectors ----------> ordered result buffer
                                                  |
                                                  v
                         bbolt transaction: observations + retry verdict + cursor
                                                  |
                                                  v
                         one read generation --> JSONL + state + report
```

Workers do not write files or mutable state. Results may finish out of order, but the
coordinator commits only the largest contiguous catalog prefix. A process stop can
waste HTTP work; it cannot move the cursor past an uncommitted module.

The first migration imports the Python JSONL/state into `go-crawl.db`. The database is
then canonical locally. Compatibility files remain the durable publish/rollback
contract and can rebuild a missing database. The catalog uses a fixed-record derived
index for the immutable prefix and keeps only post-index additions in bbolt; this
avoids duplicating roughly two million module-path strings in the database. Both the
derived index and database stay in the mounted state directory and are not built into
or published with the image.

Every module has a whole-inspection deadline in addition to each HTTP-attempt deadline.
Transient failures return to the durable retry queue; permanent proxy responses and a
failure that reaches the attempt limit move to `unavailable`. Cancellation never
crosses a missing ordered result.

Run one bounded pass explicitly:

```console
podman run --rm --init --userns=keep-id --user "$(id -u):$(id -g)" \
  --volume /path/to/go-state:/state \
  global-executables-go-crawler:local crawl --passes 1
```

`--passes 0` continues until the catalog is current, the committed cursor reaches its
end, and no retry remains. Starting the image without `crawl` is intentionally an
error, so merely building or pulling it cannot mutate production state.

The Python crawler image remains separate. During migration, orchestration sends
only the `go` source to the Go image. Rollback sends it back to the existing Python
image without converting state in place.

## One pipeline, explicit gates

`tools/go_pipeline.sh` is the only supported validation entrypoint. CI invokes the
same file used by developers.

| Gate | Command intent | Failure caught |
| --- | --- | --- |
| Toolchain | compare `go env GOVERSION` with `go.mod` | compiler drift or implicit switching |
| Dependency hygiene | `go mod tidy -diff`, `go mod verify` | stale manifests or modified module cache |
| Formatting | `gofmt` check | non-canonical source formatting |
| Static analysis | `go vet ./...` | suspicious standard-library and concurrency constructs |
| Unit/integration | `go test ./...` | functional regressions |
| Concurrency | `go test -race ./...` | exercised data races |
| Security | pinned `govulncheck ./...` | reachable known vulnerabilities |
| Build | `CGO_ENABLED=0 go build -trimpath -buildvcs=false` | non-portable or path-dependent binary builds |
| Image | build the `go-test` and final runtime stages | Dockerfile/source drift and missing runtime assets |

The race detector observes only executed paths. Concurrency tests therefore need to
exercise cancellation, retry, ordered commit, and shutdown paths rather than merely
starting workers. Fuzz targets are added for proxy escaping, ZIP metadata parsing,
and persisted-state decoding after those packages exist.

## Filesystem and cache rules

- The source tree is bind-mounted into the development container as the invoking
  user; generated files must never become root-owned.
- Go build and module caches live outside tracked source and may be deleted without
  losing durable crawler state.
- `.dockerignore` excludes `.git`, local crawl state, published data, virtual
  environments, caches, and build outputs. At discovery time the unfiltered build
  context was about 1.3 GB (`.git` 584 MB and `.local-crawl` 605 MB).
- Docker automatically applies `Dockerfile.go-crawler.dockerignore`; the shared image
  wrapper passes that same file through Podman's explicit `--ignorefile` option.
- Crawler checkpoints and observations are runtime data mounted at `/state`; they
  are never copied into a build image or a Go build cache.
- Secrets and GitHub credentials are not build arguments and are not present in
  either build stages or the runtime image.

## CI and dependency maintenance

The validation workflow has two Go boundaries:

1. A native CI job reads `go.mod`, installs the exact requested toolchain, caches by
   `go.sum`, and runs the full pipeline.
2. A container job builds the named test stage and the final image, then smoke-tests
   the produced binary. This proves that local deployment does not depend on files
   present only on a GitHub runner.

Dependency changes use Go commands rather than hand-editing version graphs:

```console
./tools/go_container.sh go get example.com/module@v1.2.3
./tools/go_container.sh go mod tidy
./tools/go_container.sh ./tools/go_pipeline.sh check
```

Every dependency update includes the `go.mod` and `go.sum` diff, tests, race check,
and vulnerability scan. The official Go base image is pinned by digest in the
Dockerfile. Updating it is an explicit reviewed change that reruns both CI boundaries.

## Migration and rollback boundary

Environment completion does not authorize a live cutover. The sequence is:

1. Make this environment and its placeholder binary green.
2. Add crawler behavior through failing tests and dependency-injected boundaries.
3. Import a copy of Python state into a new Go-owned store; never mutate the only
   rollback copy.
4. Shadow the Python crawler against a bounded catalog and compare commands,
   package identity, retry classification, cursor movement, and restart behavior.
5. Route only the Go source to the Go image. Other ecosystems remain on Python.
6. Keep the Python Go path available for one rollback window, then delete it after
   the acceptance criteria in issue #40 are satisfied.

Rollback stops the Go container and restarts the Python Go source from its last
known-good state. A rollback must not translate a partially committed Go transaction
back into the Python state file.

## Troubleshooting

- **Toolchain mismatch:** run the container entrypoint. Do not enable automatic
  toolchain download to make the check pass.
- **Root-owned files:** remove only the generated file after inventory, then confirm
  that the container is using the invoking UID before rerunning it.
- **Module drift:** run `go mod tidy`, review both manifest files, and rerun the full
  pipeline. Do not accept an unexplained checksum change.
- **Race check fails only in CI:** preserve the failing seed/log and reproduce in the
  exact development image; do not disable `-race` for the package.
- **Container build sends crawl data:** stop the build and fix `.dockerignore`; runtime
  state must never be part of the context.
- **Catalog index is missing or corrupt:** leave the module catalog intact; the derived
  `.index` file is rebuilt atomically on the next pass.
- **Compatibility export is interrupted:** restart the same Go state directory. The
  database is canonical and regenerates all compatibility views from one generation.
- **Crawler repeatedly exits nonzero:** inspect `docker logs ge-go`, preserve the DB and
  compatibility files, then use the rollback procedure in `docs/OPERATIONS.md`.

## Primary references

- [Go toolchain selection](https://go.dev/doc/toolchain)
- [Go dependency and tool management](https://go.dev/doc/modules/managing-dependencies)
- [Go security best practices](https://go.dev/doc/security/best-practices)
- [Go race detector](https://go.dev/doc/articles/race_detector)
- [GitHub `setup-go`](https://github.com/actions/setup-go/blob/main/README.md)
- [Docker build contexts and `.dockerignore`](https://docs.docker.com/build/concepts/context/)
- [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
- [Docker build best practices](https://docs.docker.com/build/building/best-practices/)
- [Podman build context and `--ignorefile`](https://docs.podman.io/en/stable/markdown/podman-build.1.html)
