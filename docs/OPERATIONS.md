# Operations and measured baseline

The current main snapshot includes a measured production OS crawl plus
explicitly partial language-registry and Homebrew inputs. The 2026-08-15
snapshot contains 63,701 unique names, 113,352 provider observations, 63,617
canonical files, and 23,714 derived index files. Debian stable, Ubuntu noble,
and Arch core are marked `exhaustive` for their declared x86_64 file indexes;
Homebrew's complete formula catalog is also marked `exhaustive` because its
official API supplies the executable inventory. npm, PyPI, crates.io, and Go
modules remain
`partial` until their package artifacts are exhaustively inspected.

The registry artifact crawler is resumable and budgeted. It enumerates the
PyPI simple catalog, follows npm's replication change cursor, pages the
crates.io catalog, and follows Go's module index cursor. For each selected
package it downloads an artifact and extracts declared console scripts, npm
bins, Cargo binary targets, or Go `package main` directories. The
`registry-artifacts.yml` workflow runs every six hours and persists its cursor,
failures, normalized observations, and report on the `artifact-data` branch.
It cannot mark a source exhaustive while any cursor, artifact, or failure
remains unresolved.

The scheduled refresh now downloads the production OS indexes with
`tools/production_crawl.py`, merges them with the currently available registry
inputs, and publishes only canonical data and reports to the `generated-data`
branch. The fixture inputs remain in the tree for parser and protocol tests;
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
  --snapshot "$(date -u +%F)" \
  --coverage-map data/production/coverage-map.json \
  --report reports/production-refresh.json
```

The scheduled refresh uses the same fail-closed production command without
developer state. It refuses a failed production source and records transfer
bytes, URL, HTTP status, duration, and per-source coverage in
`reports/production-crawl.json`.

```sh
python tools/refresh.py fixtures/intermediate/*.jsonl \
  --snapshot 2026-08-14 --coverage-kind partial
```

It writes `reports/refresh.json`, refuses missing collector inputs, validates
the generated tree, and publishes only the complete generated diff to the
`generated-data` branch. A failed source is recorded as failed and cannot be
represented as successful coverage.

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

An agent should generate candidates privately, call `check_executables`, discard
`collision` and `unknown`, similarity-check survivors, and show only survivors.
`clear_in_index` is limited to the listed snapshot and successful coverage; it
never means globally or legally available.

Collector execution status and negative-query completeness are separate.
Coverage entries are marked `fixture`, `smoke`, `partial`, or `exhaustive`.
An absent name returns `clear_in_index` only when every declared source is both
successful and explicitly exhaustive; fixture/smoke/partial snapshots return
`unknown`. A matching record always returns `collision`.

Search reads the reproducible JSON indexes rather than scanning canonical
records. Each index is SHA-256 pinned in `data/metadata.json`. A required
manifested index that is missing or whose bytes do not match fails closed with
an index error; regenerate it with `global-executables build`.

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
