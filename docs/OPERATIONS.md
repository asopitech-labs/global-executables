# Operations and measured baseline

The repository currently publishes a deterministic fixture snapshot, not a
claim of full registry coverage. The fixture baseline contains 4 unique names,
5 providers, 1 cross-ecosystem collision, 4 canonical files, and 3 successful
ecosystem inputs. It exists to make protocol and pipeline verification
repeatable. Full PyPI/crates/npm and distribution crawls remain operational
coverage work; their bandwidth, duration, rate limits, invalid records, Git file
count, and Actions duration must be measured in a scheduled run before calling
the first snapshot comprehensive.

The repository does **not** publish fixture collector output from a scheduled
workflow. The earlier fixture-based publish workflow was removed because a
fixture proves parser behavior, not upstream feasibility. `upstream-smoke.yml`
downloads representative real indexes/packages from every planned ecosystem,
inspects executable evidence inside them, and retains a measured report. It
does not claim to be a full collector or publish canonical data.

On 2026-08-14 the probe successfully downloaded and inspected Debian stable and
Ubuntu noble Contents indexes, the Arch core files database, a Homebrew bottle,
an npm tarball, a PyPI wheel, and a crates.io crate. It transferred 68,614,290
bytes in total and found the expected executable evidence in all seven sources.
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

The scheduled refresh uses the same fail-closed command without developer
state:

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
