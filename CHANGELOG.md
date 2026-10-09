# Changelog

## Unreleased

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
