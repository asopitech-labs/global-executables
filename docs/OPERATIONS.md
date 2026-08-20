# Operations and measured baseline

The current main snapshot includes a measured production OS crawl plus
explicitly partial language-registry inputs. Homebrew's complete formula
catalog is exhaustive for the supported API scope. The 2026-08-15 snapshot
contains 63,617 unique names, 113,352 provider observations, 63,617
canonical files, and 23,714 derived index files. Debian stable, Ubuntu noble,
and Arch core are marked `exhaustive` for their declared x86_64 file indexes;
Homebrew's complete formula catalog is also marked `exhaustive` because its
official API supplies the executable inventory. npm, PyPI, crates.io, Go
modules, RubyGems, and Packagist remain
`partial` until their package artifacts are exhaustively inspected.

The registry artifact crawler is resumable and budgeted. It enumerates the
PyPI simple catalog, follows npm's replication change cursor, reads the crates.io
database dump, distils Go's module index into a module catalog, enumerates RubyGems'
compact-index names catalog, and enumerates Packagist's package catalog. For
each selected package it reads authoritative package metadata, or the smallest
part of an artifact that can carry the answer, and extracts declared console
scripts, npm bins, Cargo binary targets, Go `package main` directories, RubyGems
gemspec executables, or Composer `bin` declarations. Packagist rows are emitted only when the newest package
metadata contains `bin`; the command is the basename Composer exposes through
`vendor/bin`. The
`registry-artifacts.yml` workflow runs every six hours and persists its cursor,
failures, normalized observations, and report on the `artifact-data` branch.
It cannot mark a source exhaustive while any cursor, artifact, or failure
remains unresolved.

The crawler asks each registry for the executable names before it asks for an
artifact, because most of them already publish the answer.

* crates.io states `bin_names` per version, so no `.crate` is downloaded at all.
  The database dump carries the whole registry — every crate, every version, fully
  backfilled — in one request, which is the bulk access route crates.io points
  crawlers to when they hit the API's pagination limit. crates.io reaches
  `exhaustive` in a single run.
* npm and Packagist already answered from registry metadata (`bin`).
* PyPI does not expose console scripts in its JSON API, so a wheel still has to be
  read — but a wheel is a ZIP, so `RemoteZip` reads the trailing central directory
  and then the one small member that names the commands. Wheels that ship a
  prebuilt binary declare it under `.data/scripts/` rather than as a console
  script; both are collected.
* RubyGems does not expose executables in its API and its "quick" gemspec omits
  them, so the `.gem` is still needed — but its `metadata.gz` sits at the front of
  an uncompressed tar, so the first 64KB usually suffices.
* Go needs the module source to see which directories declare `package main`.
  Every `.go` file in a directory must declare the same package, so one file per
  directory decides it, and those files are read from the archive by range.

The Go module index lists every version of every module — tens of millions of
entries, overwhelmingly republished copies of modules already inspected. Reading
the index is cheap, so a catalog phase distils the feed into distinct module paths
in `data/production/go-modules.txt` and the inspection phase spends the artifact
budget on one archive per module, at the version `@latest` reports.

Requests to the crates.io API host are paced to one per second, the rate crates.io
asks crawlers to hold; `fetch` also retries 429 with the advertised `Retry-After`.
`--source-package-budget SOURCE=N` raises the per-run package budget for a single
source, exposed as the `go_package_budget` workflow input. Each run queues its
successor and an explicit Pages deploy, because a run dispatched with
`GITHUB_TOKEN` does not emit the `workflow_run` event `pages.yml` listens for.

## Windows and .NET coverage

Linux's base command set is packaged, so Debian and Ubuntu carry it. Windows' and
macOS' are not, which is why a package-manager-only index answers "not in use" for
commands that exist on every machine of those platforms. Four sources close part of
that gap:

* **Scoop** states each package's shims in the manifest `bin` field, so ten bucket
  repositories — 1.9MB of tarballs — are read without touching an artifact.
* **MSYS2** publishes pacman databases, so the Arch reader applies unchanged; its
  databases are zstd-compressed rather than gzip.
* **winget** ships a SQLite source index whose `commands` table covers the whole
  catalog in one download. It stays `partial`: only manifests that opt in declare
  `Commands`, and the table also carries silent-install switches like `/VERYSILENT`,
  which are rejected as command names.
* **NuGet** exposes .NET tools, whose `DotnetToolSettings.xml` names the command; a
  `.nupkg` is a ZIP, so that declaration costs a range read. It stays `partial`
  because the search endpoint stops paging well short of its own `totalHits`.

Windows command names are recorded without the extension the filesystem carries.
`curl.exe` has to collide with `curl` or a cross-ecosystem index cannot answer the
question it exists for; before that normalisation MSYS2 appeared to contribute 1,426
new names, of which only 474 were genuinely new.

Distributions are not one index either. Arch keeps almost everything outside `core`,
which was the only repository being read, and Debian's `stable/main` is a fraction of
the archive; both now read every pool.

Still uncovered: the Windows System32 and macOS base command sets, which no package
manager ships. Chocolatey was investigated and rejected — its feed answers 504 to
repeated requests, its metadata never names an executable, and its packages often
contain no binary at all because the install script downloads one.

## Crawling in a container

A CI job stops at six hours and the workflow runs one at a time, so a long sweep
(Go's module catalog, npm's changes feed) is better driven locally. `Dockerfile.crawl`
builds a thin image — the package and the crawl entry point, no dataset — and
`tools/crawl_container.sh` seeds a state directory from `artifact-data`, builds the
image, and runs passes back to back until every selected source is exhaustive.

```sh
tools/crawl_container.sh                                   # every source
SOURCES=crates PASSES=1 tools/crawl_container.sh           # one source, one pass
SOURCES="go npm" PACKAGE_BUDGET=20000 tools/crawl_container.sh
```

Each pass is budgeted so the resumable state is checkpointed between passes;
stopping the container loses at most the pass in flight. Measured on the local
runtime, crates.io reaches `exhaustive` in a single pass — 319,466 crates, 88,610
executable names — at a peak of 347MB.

The container holds no credentials and never pushes. `STATE_DIR` is laid out
exactly like the `artifact-data` branch, so publishing a local run is a copy into a
worktree of that branch. Do it deliberately: the scheduled workflow writes the same
branch, and two writers produce a rejected push rather than a merge. Stop the
chained CI runs first, or crawl a source locally that CI is not advancing.

This machine runs Colima, not Docker Desktop. Two consequences:

* **Lima only shares `$HOME`.** A bind mount of any other host path silently becomes
  a directory inside the VM — writable, and invisible to the host. Keep `STATE_DIR`
  under `$HOME`; the default `.local-crawl` in the repository already is.
* `docker` needs `docker-credential-osxkeychain` on `PATH` to pull base images. If it
  is missing, point `DOCKER_CONFIG` at a directory whose `config.json` is `{}` and
  set `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`, because a replacement
  config also drops the `colima` context.

The scheduled refresh now downloads the production OS indexes with
`tools/production_crawl.py`, merges them with the currently available registry
inputs, and publishes only canonical data and reports to `main`. The fixture
inputs remain in the tree for parser and protocol tests;
they are not presented as exhaustive upstream coverage. `upstream-smoke.yml`
downloads representative real indexes/packages from every planned ecosystem,
inspects executable evidence inside them, and retains a measured report. It
does not claim to be a full collector or publish canonical data.

On 2026-08-14 the probe successfully downloaded and inspected Debian stable and
Ubuntu noble Contents indexes, the Arch core files database, a Homebrew bottle,
an npm tarball, a PyPI wheel, a crates.io crate, and a Go module archive. It
found the expected executable evidence in all eight sources.
The machine-readable observation is checked in at
[`reports/upstream-smoke.json`](../reports/upstream-smoke.json). This establishes
representative content accessibility only; it does not establish full-crawl
cost, completeness, or ongoing availability.

Local rebuild:

```sh
pip install -e '.[test]'
global-executables build fixtures/intermediate/*.jsonl --snapshot 2026-08-14
pytest
```

Production-source rebuild:

```sh
python tools/production_crawl.py \
  --source debian --source ubuntu --source arch --source homebrew \
  --source msys2 --source scoop --source winget \
  --output-dir data/production/intermediate \
  --report reports/production-crawl.json
python tools/refresh.py \
  data/production/intermediate/arch.jsonl \
  data/production/intermediate/debian.jsonl \
  data/production/intermediate/homebrew.jsonl \
  data/production/intermediate/ubuntu.jsonl \
  data/production/intermediate/crates.jsonl \
  data/production/intermediate/go.jsonl \
  data/production/intermediate/npm.jsonl data/production/intermediate/pypi.jsonl \
  data/production/intermediate/rubygems.jsonl \
  data/production/intermediate/packagist.jsonl \
  data/production/intermediate/msys2.jsonl \
  data/production/intermediate/scoop.jsonl \
  data/production/intermediate/winget.jsonl \
  data/production/intermediate/nuget.jsonl \
  --snapshot "$(date -u +%F)" \
  --coverage-map data/production/coverage-map.json \
  --report reports/production-refresh.json
```

The scheduled refresh uses the same fail-closed production command without
developer state. It refuses a failed production source and records transfer
bytes, URL, HTTP status, duration, and per-source coverage in
`reports/production-crawl.json`.

## GitHub Pages playground

The `pages.yml` workflow builds the static index playground and deploys it with
the GitHub Pages artifact/deploy actions. It runs after pushes to `main`, after
registry crawls, and after generated refreshes. During the build it snapshots
the latest `artifact-data` crawl report into `status.json`, including the next
scheduled crawl time, source cursors, failures, and coverage kind.

The browser client reads canonical metadata and executable/index shards from
the public `main` JSON tree. It implements the read-only query surface for
exact checks, batch checks, provider reads, bounded prefix/scope searches,
similar-name searches, coverage, and freshness assessment. No query payload is
sent to an application server. The repository Pages setting must use
**GitHub Actions** as its source the first time the workflow is enabled.

```sh
python tools/refresh.py fixtures/intermediate/*.jsonl \
  --snapshot 2026-08-14 --coverage-kind partial
```

It writes `reports/refresh.json`, refuses missing collector inputs, and
validates the generated tree. This local command does not publish to GitHub;
the scheduled `refresh.yml` workflow runs the production equivalent and
publishes the complete generated diff to `main`. A failed source is recorded
as failed and cannot be represented as successful coverage.

Real-content smoke test (network, substantial downloads, and Cargo required):

```sh
pytest -m live -o addopts= -q
python tools/live_smoke.py --output reports/upstream-smoke.json
```

Local MCP (no network):

```sh
global-executables-mcp --root .
```

Remote Streamable HTTP:

```sh
global-executables-mcp --root . --transport streamable-http --host 0.0.0.0 --port 8000
```

An agent should generate candidates privately and call `check_executables`. It
should discard factual collisions, retain `found:false` candidates together
with their absence confidence, similarity-check those not-found candidates,
and show only survivors with snapshot and coverage caveats. `unknown` means
insufficient coverage, not an unavailable query. `clear_in_index` requires an
explicitly exhaustive queried snapshot; it never means globally or legally
available.

Collector execution status and negative-query completeness are separate.
Coverage entries are marked `fixture`, `smoke`, `partial`, or `exhaustive`.
An absent name returns `clear_in_index` only when every declared source is both
successful and explicitly exhaustive; fixture/smoke/partial snapshots return
`unknown`. A matching record always returns `collision`.

Search reads the reproducible JSON indexes rather than scanning canonical
records. Each index is SHA-256 pinned in `data/metadata.json`. An index listed
in the manifest that is missing or whose bytes do not match fails closed with
an index error; a filter value with no corresponding index produces no
candidates. Regenerate indexes with `global-executables build`.

Provider scope dimensions are independent: OS records may include
`source_type`, `package_system`, `distribution_family`, `distribution`, and
`distribution_release`; language records may include `language` and `registry`.
Freshness and usage observations are provider facts. Missing metrics are
`unknown`, not zero, and cross-ecosystem counts are never compared without a
documented normalization method. `assess_executable` exposes derived risk with
methodology version `1.0.0` while preserving the underlying observations.

## Incremental freshness scans

Full crawls are not required for every freshness observation. A partition
manifest under `fixtures/freshness/manifest.json` declares the source units to
visit. A bounded run advances a persisted round-robin cursor, scans a limited
number of partitions and normalized records, and writes:

- `data/freshness/state.json` — cursor, source checksums, completed cycles,
  last successful observations, and staleness state;
- `reports/freshness.json` — run ID, selected/skipped/failed partitions,
  source checksum, cursor, bytes, changes, removals, and rate-limit metadata.

Run it locally with:

```sh
global-executables freshness \
  --root . \
  --manifest fixtures/freshness/manifest.json \
  --state data/freshness/state.json \
  --report reports/freshness.json \
  --partition-budget 1 \
  --record-budget 1000
```

The scheduler is deterministic and fair: every enabled partition is selected
in manifest order over successive runs, including partitions skipped by a
previous run budget. A source checksum change starts a new cycle. New and
changed observations are reported immediately; removals are reported only when
the changed source partition completes a full cycle. A missing or malformed
source preserves the last successful observation and marks the partition
stale/unavailable.

Freshness output is intentionally separate from `data/executables`. A partial
scan always remains `coverage_kind=partial`, cannot make an absence
`clear_in_index`, and cannot set dataset metadata to exhaustive. The scheduled
workflow publishes only state and the report to the `freshness-data` branch;
failed partitions remain visible and fail the workflow after the report is
published. The workflow is a freshness signal, not a full-coverage claim.
