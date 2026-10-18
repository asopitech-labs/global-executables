# Changelog

## Unreleased

- Booster/Actions: failures of packages missing from the catalogue (created after it was
  taken and known only from the change feed, e.g. `libcuopt-*`) were retried by nobody once
  a booster held their bucket. They now belong to the feed reader (Actions); catalogue
  packages stay with the owner of their bucket, so every retry has exactly one owner. A
  delta publish now also removes failure entries the writer resolved (a newer check, or a
  final `unavailable`) even when its base never had them; the schedule cache is kept with
  the base for this. `tools/booster.py owners` (in `local_booster.sh status`, the publish
  log and the lease progress) counts failure entries with no owner or two.

- PyPI: projects whose `/pypi/<name>/json` exceeds the 8 MiB metadata limit (pydantic_core,
  grpcio, aiohttp, ddtrace, zeroconf and 10 more) failed three times and dropped out as
  unavailable. Such a project is now read with a range request for the head of that
  document (`info.version`; an unchanged version stops there) and the release document
  `/pypi/<name>/<version>/json` (50-250 KB) for the files. The HTTP/2 stream window is
  512 KiB, which bounds what an abandoned oversized response still transfers. Packages given
  up or queued for this reason are requeued once with a fresh attempt count (a recovery
  rule keyed on the failure reason); nothing else is rewritten.
- Refresh/booster duplicate work and endless retries (refs #64):
  - Retries follow bucket ownership: the Actions refresh (`--rotation-exclude`) no longer
    retries packages in a local booster's buckets unless the change feed announced them; the
    booster keeps retrying its own. Since 10/14 Actions retried about 375 booster-owned
    packages in every run.
  - A failure of a feed, walk or rotation work reset the package's attempt count to 1, so
    packages that always fail never reached `FailureAttemptLimit`. The count now continues
    from the recorded one, and publication merges `failure_attempts` by maximum.
  - PyPI artifacts larger than the limit (by PyPI's JSON `size` or `Content-Length`, now
    checked before downloading) or with an invalid tar header are final for that version:
    recorded as checked with the new outcome `u` (unreadable), not retried, and read again
    only when the version changes or `ExtractionRevision` rises. Reports list `unreadable`
    only when there is one.
- Local booster: after the machine was suspended, the PyPI crawler kept sending requests over
  a silently dead HTTP/2 connection (each one timing out and retrying on it), and the
  supervisor, which looked at the crawler only once per hour of its own sleep, neither noticed
  the stall nor promptly relaunched the crawler after it exited. HTTP/2 connections are now
  health-checked with pings (dropped after 15 s + 10 s of silence). The supervisor checks every
  `WATCH_INTERVAL` (60 s), publishes and heartbeats by the wall clock, and restarts a crawler
  whose `--progress-file` has not moved for `STALL_TIMEOUT` (1800 s) after a SIGQUIT goroutine
  dump; stalls count towards `CRASH_LIMIT`.
- Local booster: a bbolt store damaged by a file-level snapshot/restore of the live file
  (`invalid page type` panic in `BoltStore.Commit` after a reboot) crash-looped the crawler.
  New `--verify-store` (used by the booster) checks the store on first open, quarantines a
  damaged one as `<db>.corrupt-<timestamp>` and rebuilds it from the atomic exported state;
  a later corruption panic quarantines it and exits 75. The supervisor restarts an exited
  crawler and, on a crash loop (`CRASH_LIMIT`/`CRASH_WINDOW`), publishes, releases the lease
  and exits 1. New `local_booster.sh <source> abort` stops without a final publication and
  keeps the lease. fsync was already on (no `NoSync`).
- Local booster, native mode: publications failed with "Podman or Docker is required"
  because `crawl_parallel.sh` refused to load without a container runtime even for `seed`,
  `merge` and `publish`. The runtime is now required only by the commands that use it, and
  `MODE=native` sets `NO_CONTAINER=1`. New `tools/booster.py coverage` (also in
  `local_booster.sh status`) accounts for every catalogue position (checked by outcome,
  unavailable, failing, not reached yet ahead of/behind the rotation cursor).
- Local booster, native mode: the crawler's PID was recorded as that of a waiting subshell,
  so on SIGTERM the supervisor could not stop the crawler and the final publication raced
  with it. The PID file now names the crawler; stop waits for it to exit (KILL after
  `STOP_TIMEOUT`) before the final publication and the lease release, stale and foreign PID
  files are ignored, and a second start (or a crawler without a PID file) is refused.
  `LEASE_TTL_HOURS` is documented, and the lease heartbeat is renewed independently of the
  publish interval (`HEARTBEAT_INTERVAL`, `LEASE_MIN_AGE`; `booster.py renew --min-age-hours`
  commits only when the lease is getting old).
- Local booster for PyPI (#69, refs #64): `tools/local_booster.sh pypi run` crawls the
  never-visited backlog (588,000 of 870,264 packages) from your machine in parallel with
  Actions. Ownership by bucket (`sha256(name)[0]`, Go `OwnerBucket` / Python
  `owner_bucket`) with a lease and heartbeat on `artifact-data`
  (`data/production/booster/<source>.json`, 12 h TTL); while it is alive
  `registry-refresh.yml` passes `--rotation-exclude`, afterwards Actions owns everything
  again. Both writers publish deltas (`DELTA_SOURCES=pypi`: per-package state entries and
  the rows of changed packages only), so neither erases the other; re-publishing is a
  no-op. New crawler flags `--rotation-include/-exclude`, `--no-feed`, `--user-agent`
  (`GE_USER_AGENT`), `--max-bytes-per-second`, `--pause-jitter`.
- Packages without commands are recorded like the others (#68, refs #64): the schedule
  cache entry is now `<day>:<streak><outcome>:<version>` (`n` = no commands, `c` = has
  commands; entries without a letter still parse) for every inspected package. Go used to
  record a check only for modules with rows and read every other module in full on each
  visit; it now records all of them. Reports gain `outcomes` (and `read_no_commands` /
  `read_with_commands` for Go). Go imports checks into its working database in key order
  (1,000,000 entries: over ten minutes before, about 3 s now). Cold-start seeds from rows
  are `c`. Cache format version unchanged; golden files updated. `tools/measure_negative_history.py`
  measures why a git-tracked negative history was not chosen.
- Schedule cache now survives CI (#67, refs #64): the first scheduled `registry-refresh`
  runs after #66 never saved their cache (`tar: Cannot open: Permission denied`, every run a
  cold start) because the Go crawler container, running as root, wrote a 0600 file under
  `/tmp` that the runner-user cache action could not read. The cache directory is now the
  workspace-relative `data/production/cache`, created by the runner and mounted into the
  container; cache files are written 0644; the cache steps use a per-attempt key, tolerate
  failure, and a step hands the files back to the runner.
- History and cache are separate (#66, refs #64): per-package check bookkeeping
  (`checked`, the refresh rotation position) is no longer written to the registry
  state. It lives in a schedule cache, `data/production/cache/<source>.cache.gz`, kept
  by `actions/cache` and never committed; a lost cache only makes every package due
  again (staggered, history untouched). A check that finds a package unchanged writes
  nothing to git, so a run where nothing changed publishes nothing: an all-unchanged
  3,000-package PyPI batch went from 256 shard files / 52,864 B to 0 / 0. The
  per-run effort fields of the report no longer trigger a commit; an idle run
  republishes only the report (last-crawl time) at most once per 24 h
  (`REPORT_HEARTBEAT_HOURS`, default 24, 0 disables), so the status page keeps updating. `snapshot_generation` moves only when history did.
  Legacy `checked` is read for one release and dropped on the first save. See "History
  and cache" in docs/OPERATIONS.md.
- Change-driven refresh (#64, refs #58): the latest-version refresh skips packages
  whose registry version is unchanged (Go, npm, PyPI, RubyGems, Packagist, NuGet, Conan),
  backs off unchanged packages exponentially without spending budget, and uses the
  PyPI, npm, Packagist, Conan and NuGet change feeds to queue changed packages first (NuGet
  polls the V3 catalog by commit timestamp, handles deletes, resyncs past 24 pages).
  The rotation remains the backstop. New registry-state fields `feed_cursor`,
  `feed_pending`, `due_floor`, `extraction_revision` (generic maps; Python and Go
  encoders byte-identical, golden in `internal/gocrawl/testdata/refresh/state-golden`). See "Change-driven
  refresh" in docs/OPERATIONS.md.
- vcpkg/xmake publication: the #60 replace-on-reparse fix only reached the collector's
  own output. `crawl_parallel.sh publish` merged that output into `artifact-data` by
  row identity and re-added every row the collector had dropped, so the 2026-10-08
  cpp-registries run (7787cd0) still published mnn `cpp`/`train`, openexr
  `not`/`exrcheck`, and xmake `autotools`/`binutils` (926 vcpkg and 117 xmake rows
  instead of 914 and 104). The collector now names the packages it re-parsed in a
  `<source>.reparsed.json` sidecar, and the new `tools/merge_observations.py` drops
  their published rows before merging. Other observation sources keep accumulating
  (#58).
- Go toolchain: 1.26.7 → 1.26.9 (`go.mod` toolchain line and the digest-pinned
  `golang:1.26.9-bookworm` image in `Dockerfile.go-crawler`). `govulncheck` in the
  `go` CI job reported reachable `net/http` vulnerabilities fixed in go1.26.9
  (GO-2026-6617 and related advisories published 2026-10-08), which failed `main`.
  `golang.org/x/mod` moves from v0.39.0 to v0.40.0 so `govulncheck` also stops
  listing the unreached `sumdb` advisories GO-2026-6179 and GO-2026-6180.
- Registry crawl state: `data/production/registry-state.json` (92.7 MB on
  `artifact-data`, near GitHub's 100 MB file limit) is replaced by the
  `data/production/registry-state/` directory: a `manifest.json` commit point, one
  `source.json` per registry, and large maps such as Go's 1.18 million `unavailable`
  modules as SHA-256-prefix JSONL shards (281 files, largest 331 KB). Python and Go
  write byte-identical files, saves are atomic and rewrite only changed shards, and a
  typical publication pushes 0.7–1.5 KB instead of 3–4 KB. The first publication after
  this change migrates the branch in its normal commit; readers fall back to the
  legacy file for one release. Shell steps use `tools/registry_state.py`
  (`restore`, `get`, `copy`, `summary`, `migrate`). See "Registry crawl state layout"
  in docs/OPERATIONS.md.
- ConanCenter: `bin/` entries that are never commands are no longer recorded
  (`LICENSE`, `COPYING`, `OWNERS`, `PKG-INFO`, `setup.cfg`, `pyproject.toml`,
  `*.exe.config`, `*.jar`, `*.conf`, versioned `.so` libraries, ...). Scripts are kept
  (#58).
- xmake: the recorded version is the newest declared one rather than the last
  `add_versions` line (`meson` was published as 0.50.1 instead of 1.12.1), and bundle
  packages such as `autotools`, `binutils`, and `*-tools` no longer yield an inferred
  command named after the package (#58).
- ConanCenter: single-quoted versions in `config.yml` are unquoted (`fff/'1.1'` was
  requested verbatim and returned 404), and a recipe whose newest version is not
  published or not built falls back to at most three older published versions
  (`gcc/16.1.0` now yields gcc 15.x/12.x commands). Conan rows record the inspected
  `version` alongside the declared `latest_version`. Recipes still `unavailable` count
  against completeness (#58).
- ConanCenter: the recipe catalogue is re-read every seven days once the walk is
  complete, and a changed catalogue rolls over (new recipes and versions are queued in
  `catalog_pending`) instead of failing with "catalog changed". The catalogue had not
  been re-read since 2026-09-14 and was missing 3 recipes and 78 version updates (#58).
- vcpkg: CMake comments are ignored when reading `vcpkg_copy_tools(TOOL_NAMES ...)`.
  Commented names were published as commands (`mnn`: `cpp`, `converter`, `train`, ...;
  `openexr`: `not`, `exrcheck`). Re-parsed vcpkg and xmake packages now replace their
  older rows instead of accumulating them, so the next scheduled crawl removes those
  rows from `artifact-data` (#58).
- Moved the generated dictionary to the orphan `dictionary` branch. Consumers
  of `raw.githubusercontent.com/.../main/data/...` must change the branch segment
  to `dictionary`; raw GitHub URLs do not redirect across this cutover.
