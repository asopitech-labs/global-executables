# Operations and measured baseline

The durable operating model has separate sources of truth: normalized observations and
crawl state live on `artifact-data`; the queryable dictionary and its derived indexes
live on the orphan `dictionary` branch; program source lives on `main`. A refresh seeds every observation before collecting, preserves stored
evidence when an upstream source fails, and refuses an unexplained decrease in unique
names. Invalid individual command names are quarantined in the refresh report rather
than stopping publication; malformed input and unexplained shrinkage still block it.

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
* npm answers from registry metadata, but only from the release document: a bare
  package name returns every version ever published, where `bin` never appears at the
  top level. Packages are enumerated once from `_all_docs` — the changes feed carries
  126 million revisions against 4.3 million packages, so reaching each package through
  it costs thirty times what listing them does.
* Packagist already answered from registry metadata (`bin`).
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

## Registry crawl status

Where the artifact crawl stood on 2026-08-21. The catalogue percentage is the cursor
against the enumerated name list, not against rows collected — most packages ship no
executable, so rows grow far more slowly than the cursor.

| Source | Coverage | Cursor | Rows | Runs on |
| --- | --- | --- | --- | --- |
| crates.io | `exhaustive` | 319,955 / 319,955 | 88,771 | CI, one dump per run |
| NuGet | `exhaustive` | 9,190 / 9,190 queries | 13,143 | local, finished |
| RubyGems | `partial` | 104,790 / 196,126 (53.4%) | 27,298 | local container |
| Packagist | `partial` | 225,856 / 458,432 (49.3%) | 6,698 | local container |
| PyPI | `partial` | 116,963 / 870,264 (13.4%) | 47,060 | local container |
| npm | `partial` | 155,181 / 4,311,362 (3.6%) | 23,957 | local container |
| Go | `partial` | 7,792 / 1,965,638 (0.4%) | 132,911 | local container |

Only crates.io runs in CI: its whole registry arrives in one dump, so it finishes
inside a job. Every catalogue-walking source is filled by `crawl_parallel.sh` on a
local machine, because a CI job stops at six hours and the workflow runs one at a
time. `watch` publishes to `artifact-data` on an interval so the progress does not
live on one machine.

Go's catalogue phase is complete. Every new pass now spends its budget on inspecting
modules. Its row count is large against its cursor because a module contributes one row
per `package main` directory, and because the catalogue phase ran long before the
inspection phase started.

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
  `.nupkg` is a ZIP, so that declaration costs a range read. One search query stops
  paging at 4,000 of the 9,190 tools it advertises, but that cap is per query rather
  than per catalogue: the empty term plus a–z and 0–9 reach every one of them, and the
  union matches the advertised count exactly. Pre-release-only tools are included,
  since a command name is taken either way.

Windows command names are recorded without the extension the filesystem carries.
`curl.exe` has to collide with `curl` or a cross-ecosystem index cannot answer the
question it exists for; before that normalisation MSYS2 appeared to contribute 1,426
new names, of which only 474 were genuinely new.

Distributions are not one index either. Arch keeps almost everything outside `core`,
which was the only repository being read, and Debian's `stable/main` is a fraction of
the archive; both now read every pool.

The Windows base command set is not packaged by anything, so it is read from the
official images instead — a container layer is an ordinary tar, so Microsoft's
`servercore` and `nanoserver` images are inspected without running Windows. The tag
moves and the digest does not, so each record cites the image digest and the release
it was shipped in, which pins that release's command set reproducibly. Names fold to
lower case there: the images ship `ARP.EXE` beside `attrib.exe`, and on a
case-insensitive filesystem the command a user types has to collide with the `arp`
every Linux index already carries.

What makes a file a command is that its directory is on the default PATH, which is
wider than System32: `Wbem` holds WMIC, and PowerShell and the bundled OpenSSH client
each sit in their own directory. Reading System32 alone missed all of them.
servercore ltsc2022 yields 347 commands, 295 of them new to the dataset.

Two gaps remain, and neither is reachable by inspecting a filesystem:

* **Shell built-ins and aliases.** `path`, `dir`, `copy` and `set` are cmd.exe
  built-ins, and `ls`, `cat` and `curl` are PowerShell aliases; none exists as a file.
  The same is true of `cd`, `echo` and `test` under bash. The count is small but the
  names are short and heavily contested, so their weight is not proportional to it.
macOS' base command set is read the same way, from an installed system rather than a
published artifact, because Apple publishes no manifest for it. `/etc/paths` states
the default PATH, `/usr/local/bin` is excluded as a third-party prefix that Homebrew
already covers, and the observation is pinned to `ProductBuildVersion` and the
architecture — a build identifies a release as precisely as a digest does, and the
command set differs between Apple Silicon and Intel.

No observation of a base command set is privileged: a maintainer's laptop and a CI
runner are both single samples, so `base-commands.yml` adds runner images to whatever
is observed locally and the samples accumulate. Every record cites the build it came
from, so a second machine, architecture or release widens coverage rather than
replacing it. macOS 26.5.2 (25F84, arm64) contributes 1,258 commands, 586 of them new.

A system binary that refuses `stat` to an unprivileged reader is still recorded: the
name occupies a PATH directory, and dropping it would turn "could not read" into
"name is free". Chocolatey was investigated and rejected — its feed answers 504 to
repeated requests, its metadata never names an executable, and its packages often
contain no binary at all because the install script downloads one.

## Interruption and resume

A crawl is expected to be stopped: a job hits six hours, a container is stopped, a
run is cancelled. What matters is what an interruption costs.

State used to be written once, after every source in the run had finished, so a kill
anywhere discarded the cursors of the sources that had already completed as well as
the work in flight — and because rows are appended per source, those sources would
re-crawl and write their observations twice. State is now saved after each source,
and every 200 packages inside a source, together with the rows collected so far, so
the cursor and the observations always agree.

`SIGTERM` and `SIGINT` no longer kill the process between checkpoints, which is when
the unsaved work is largest. They set a flag the crawl loops check, so the run stops
at its next checkpoint and reports `interrupted`.

A catalogue is what a pass cannot cheaply redo — npm's costs 431 requests, NuGet's
37 queries, Go's thousands of index pages — so each is persisted the moment it is
built rather than when the pass returns. Without that, a truncated response during
inspection unwound the pass and discarded a catalogue that was already complete on
disk. A test asserts every crawler does this, because the same gap was found and
closed three separate times.

crates.io is asked for the dump's `Last-Modified` before downloading it. The dump is
republished daily and its observations replace rather than extend, so re-reading an
unchanged one spent 1.7GB to rewrite an identical file on every run.

## When a package is gone

A registry keeps listing packages it will no longer serve. Every one of them is a
failure that no retry can resolve, and a source cannot reach `exhaustive` while a
failure is outstanding — so misclassifying one holds a whole registry back forever.
Failures are therefore split in two: `failures` are worth another attempt,
`unavailable` records a settled negative answer and is a result, not a loss.

Each registry expresses withdrawal differently, and the difference was measured
rather than assumed:

| Registry | How withdrawal appears | Handling |
| --- | --- | --- |
| crates.io | `yanked` per version in the dump | yanked versions are filtered before a crate is considered |
| RubyGems | API reports `yanked: true`, CDN answers `403 AccessDenied` for the artifact | the flag is read first, so the artifact is never requested |
| npm | unpublished packages 404 on `/<name>/latest` | permanent on the first answer |
| PyPI | yanked releases keep serving their files; `info.version` already skips them | nothing to skip; a release with no wheel or sdist is permanent |
| Go | the proxy excludes retracted versions from `@latest` | nothing to skip; unknown modules 404 |
| NuGet | the search catalogue omits unlisted packages; deleted ones 404 | permanent on the first answer |
| Packagist | removed packages 404 | permanent on the first answer |

The two that need the flag read first are the two where metadata answers `200` while
the artifact host refuses. Fetching those bought a `403` on every pass: 38 yanked gems
were retried indefinitely before the flag was consulted.

`404` was once the only answer treated as final. Three more are:

* `410` and `451` — how a registry reports a withdrawal or a legal takedown.
* `405` — what npm returns for the package literally named `-`, whose path collides
  with the registry's own `/-/` API namespace.

Anything else that is not a network error gives up after `FAILURE_ATTEMPT_LIMIT`
attempts and is recorded as `gave up after N attempts: <reason>`. Without a bound, one
truncated artifact keeps a source `partial` permanently. Network errors are exempt on
purpose: they say nothing about the package, and a DNS outage spanning a few passes
would otherwise bury packages that are perfectly readable. That exemption is not
theoretical — of twelve RubyGems failures outstanding at one restart, eleven were
transient name-resolution errors that succeeded on the next attempt.

Which is also why every crawler now revisits its failures. Three did not. RubyGems and
npm had no queue at all, and PyPI's held only projects owed an sdist read, so any other
failure was left behind the cursor with nothing to bring it back. Since completion is
refused while a failure stands, that was not merely lost data — those sources could
never have reached `exhaustive` at all, and nothing in the report said so.

Each retry queue is bounded to its length at the start of the pass, so a package that
fails again is revisited next pass rather than consuming this pass's budget over and
over. The bound and the queue are a pair: without the queue an unreadable artifact is
never re-attempted, so the attempt limit never fires and the deadlock stands.

`test_every_crawler_that_blocks_on_failures_can_revisit_one` asserts the invariant
directly — a crawler that gates completion on `not failures` must put a recorded
failure back on a retry queue. It is a test rather than a note because the same gap was
found in three crawlers separately, each time by reading one of them for another reason.

## Crawling in a container

A CI job stops at six hours and the workflow runs one at a time, so long catalog
sweeps are driven locally. `Dockerfile.crawl` remains the Python runtime for non-Go
registries. Go uses `Dockerfile.go-crawler`, a dedicated transactional runtime.
`tools/crawl_container.sh` is the single Python-container path;
`tools/crawl_parallel.sh` routes each source to its correct image.

```sh
tools/crawl_container.sh                                   # every source
SOURCES=crates PASSES=1 tools/crawl_container.sh           # one source, one pass
SOURCES="npm" PACKAGE_BUDGET=20000 tools/crawl_container.sh
```

`tools/crawl_parallel.sh` runs one container per registry instead. Every
catalog-walking source is filled this way; CI keeps only crates.io, whose whole
registry arrives in a single dump and therefore finishes inside a job. Each registry
answers from its own hosts, so crawling them serially spends most of the wall clock
waiting on one of them; four containers together held 16% of the VM's two cores,
because the work is entirely I/O. Every container gets its own state directory — a
shared one would put four writers on a single cursor file — and `merge` folds the
per-source states back into one publishable tree.

A pass whose cursor has reached the end of its catalog returns instantly having
processed nothing, so the loop backs off rather than retrying every few seconds, and
stops once several consecutive passes have achieved nothing. NuGet reached that state
after 4,000 tools and was spinning through a pass every five seconds.

Each pass is budgeted. Python sources checkpoint on their existing cadence. The Go
runtime commits ordered batches transactionally, so stopping it loses only uncommitted
HTTP work, not the entire pass. Measured on the local runtime, crates.io reaches
`exhaustive` in a single pass — 319,466 crates, 88,610 executable names — at a peak of
347MB.

### Transactional Go runtime

`SOURCES=go tools/crawl_parallel.sh start` builds the shared Python image and the
dedicated Go runtime, then starts `ge-go` with `crawl --passes 0`. A nonzero local
failure is restarted up to five times; reaching exhaustive exits zero and is not
restarted. Other `ge-*` containers still run Python.

The mounted Go state directory contains:

- `registry-state.json`, `intermediate/go.jsonl`, and the report: publishable,
  Python-compatible snapshot views;
- `go-crawl.db`: the canonical local transaction store; and
- `go-modules.txt.index`: a rebuildable exact membership index for the immutable
  catalog prefix.

The DB and derived index are local operational state and are not published. The module
catalog is replaced atomically when new names arrive from `index.golang.org`; a stop
between catalog replacement and DB metadata commit is reconciled from the file tail on
restart.

`crawl_parallel.sh publish` merges only the state and crawl-report entries this machine
owns, and refuses a source whose local cursor is behind the published one — the check
that stopped Go's catalogue being rolled back fifteen months. The scheduled crates
writer uses the same source-owned merge and publishes only `crates.jsonl`; it must not
copy a stale whole-state or whole-report snapshot over other writers. `watch` runs the
publisher on an interval, because publishing by hand leaves the crawl's progress on one
machine until someone remembers. The merged report is what Pages exposes as
`status.json`, so a published cursor and its public progress display advance together.

The single-source container holds no credentials and never pushes. `STATE_DIR` is laid out
exactly like the `artifact-data` branch, so publishing a local run is a copy into a
worktree of that branch. Do it deliberately: the scheduled workflow writes the same
branch, and two writers produce a rejected push rather than a merge. Stop the
chained CI runs first, or crawl a source locally that CI is not advancing.

## Checking a running crawl

`tools/crawl_parallel.sh status` gives the cursor per source. Python sources resume
from their state files. Go resumes from bbolt and regenerates its state file as the
publishable view; the report is only a summary of the last pass.

```sh
tools/crawl_parallel.sh status                    # container status and cursor
docker logs --tail 20 ge-rubygems                 # per-pass summary lines
docker logs --tail 20 ge-go                       # transactional Go passes
git show origin/artifact-data:reports/registry-artifact-crawl.json | jq '.sources'
```

`status` shows `cursor/catalog_size` when the collector exports both values. For older
source snapshots without `catalog_size`, compare the cursor with the catalogue file:

```sh
python3 - <<'PY'
import gzip, json, pathlib
for src, cat in (("pypi", "pypi-projects"), ("npm", "npm-packages"), ("go", "go-modules"),
                 ("rubygems", "rubygems-names"), ("packagist", "packagist-packages")):
    d = pathlib.Path.home() / f".ge-crawl-{src}/data/production"
    # Every source but Go reads the compressed copy first; Go updates the plain file
    # atomically and never gzips it, so a .gz beside it would be stale by definition.
    order = (".txt",) if src == "go" else (".txt.gz", ".txt")
    f = next((d / f"{cat}{e}" for e in order if (d / f"{cat}{e}").is_file()), None)
    if not f:
        continue
    total = sum(1 for _ in (gzip.open if f.suffix == ".gz" else open)(f, "rt"))
    s = json.loads((d / "registry-state.json").read_text())["sources"][src]
    print(f"{src:10} {s.get('cursor', 0):>9,}/{total:<9,} {s.get('cursor', 0) / total:6.1%}")
PY
```

Go refreshes its denominator from the chronological module index before each pass.
`catalog_complete` means the index reader reached the current edge for that snapshot;
new module versions may make it grow on a later pass.

## Go cutover and rollback

Cut over only after the shared Go pipeline, Go test/runtime image stages, a copied-state
shadow run, restart test, and Python dictionary rebuild all pass.

1. Publish or copy the final Python checkpoint and record its cursor, JSONL row count,
   failure count, container image, and timestamp.
2. Stop only `ge-go`; leave every other registry container running.
3. Preserve the compatibility state, Go JSONL, catalog, and previous image as the
   rollback snapshot.
4. Start Go through `SOURCES=go tools/crawl_parallel.sh start` against that same source
   directory. The first pass imports the compatibility snapshot into a new DB.
5. Verify that the first exported cursor is not below the recorded cursor, generated
   files are owned by the host user, failures do not persist, and `docker logs ge-go`
   reports nonzero processing.
6. Publish only after `tools/crawl_parallel.sh publish` passes its no-regression check.

Rollback stops `ge-go`, restores the recorded compatibility snapshot, and starts the
previous Python crawl image with `SOURCES=go`. Do not copy a partially written DB into
the rollback directory. Keep the Go DB separately for diagnosis; Python does not need
or read it.

To check that withdrawals are being classified rather than retried, group the
outstanding failures by kind. A kind that recurs across passes without changing is a
condition the crawler is not recognising — that is how the RubyGems `403` storm and
npm's `405` were found:

```sh
python3 - <<'PY'
import collections, json, pathlib, re
for p in sorted(pathlib.Path.home().glob(".ge-crawl-*/data/production/registry-state.json")):
    for name, s in json.loads(p.read_text()).get("sources", {}).items():
        f = s.get("failures", {})
        if not f:
            continue
        print(f"{name:10} failures={len(f):<4} unavailable={len(s.get('unavailable', {})):<6} "
              f"attempts={len(s.get('failure_attempts', {})):<4} "
              f"queued={len(s.get('retry_gems') or s.get('retry_projects') or s.get('retry_modules') or [])}")
        kinds = collections.Counter(
            (m.group(0) if (m := re.search(r"HTTP Error \d{3}", v)) else v.split("(")[0][:40])
            for v in f.values())
        for kind, n in kinds.most_common(5):
            print(f"           {n:>4}  {kind}")
PY
```

`failure_attempts` counts only failures that are neither settled nor network errors, so
a healthy source has it at or near zero while `failures` fluctuates with connectivity.
An entry in `unavailable` beginning `gave up after` is a package the crawler could not
read after `FAILURE_ATTEMPT_LIMIT` tries; those are worth reading occasionally, because
a cluster of them sharing one reason is a parser gap rather than a set of bad packages.

The first four to appear were all PyPI projects reading `File is not a zip file`, and
they split three to one. `Abr1k0s` is 21 bytes of SVG and `Jeiji` is a RAR archive named
`.tar.gz` — genuine rubbish, correctly settled. `Task_allocator` is a well-formed
tarball that PyPI records as a `bdist_wheel`, so the crawler read it as a ZIP and failed
on an artifact whose commands an sdist read finds immediately. The reader is now chosen
by the filename rather than by the declared type. Reading the give-up list is how that
was found; the count alone said nothing.

Before trusting any of it, confirm the containers are on the build you think they are.
A state file written by the previous container survives the restart, so reading it too
early shows the old shape and looks like the fix did not take:

```sh
docker inspect ge-rubygems --format '{{.Image}} {{.State.StartedAt}}'
docker images global-executables-crawl:local --format '{{.ID}} {{.CreatedSince}}'
```

This machine runs Colima, not Docker Desktop. Two consequences:

* **Lima only shares `$HOME`.** A bind mount of any other host path silently becomes
  a directory inside the VM — writable, and invisible to the host. Keep `STATE_DIR`
  under `$HOME`; the default `.local-crawl` in the repository already is.
* `docker` needs `docker-credential-osxkeychain` on `PATH` to pull base images. If it
  is missing, point `DOCKER_CONFIG` at a directory whose `config.json` is `{}` and
  set `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`, because a replacement
  config also drops the `colima` context.

## Durable observation lifecycle

The scheduled refresh performs the lifecycle in this order:

1. Restore every OS, registry, macOS, and shell observation from `artifact-data`.
2. Collect the production OS indexes. A successful observation is merged into the
   stored evidence; if an upstream source fails, a non-empty stored copy is retained
   and the crawl report is marked `degraded`. A missing fallback remains a hard failure.
3. Publish the refreshed OS observations back to `artifact-data`.
4. Rebuild the dictionary. Unusable command names are counted under
   `rejected_records` in `reports/production-refresh.json`; malformed JSON remains a
   hard failure.
5. Refuse to replace the dictionary when the new unique-name count is lower. An
   intentional removal requires `--allow-shrink-reason "..."`, which is recorded in
   the refresh report.
6. Publish canonical data and reports to `dictionary`. A failed scheduled refresh opens or
   updates a GitHub issue with the failed run URL.

For a local OS crawl, seed first so the collector extends durable evidence, then
publish the result:

```sh
tools/crawl_parallel.sh seed
python tools/production_crawl.py \
  --source debian --source ubuntu --source arch --source homebrew \
  --source msys2 --source scoop --source winget --source windows \
  --output-dir data/production/intermediate \
  --report reports/production-crawl.json
tools/crawl_parallel.sh publish
```

The fixture inputs remain in the tree for parser and protocol tests;
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
global-executables build fixtures/intermediate/*.jsonl \
  --root /tmp/global-executables-fixture --snapshot 2026-08-14
pytest
```

Production-source rebuild:

```sh
python tools/production_crawl.py \
  --source debian --source ubuntu --source arch --source homebrew \
  --source msys2 --source scoop --source winget --source windows \
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
  data/production/intermediate/windows.jsonl \
  data/production/intermediate/macos.jsonl \
  data/production/intermediate/shell.jsonl \
  data/production/intermediate/nuget.jsonl \
  --snapshot "$(date -u +%F)" \
  --coverage-map data/production/coverage-map.json \
  --report reports/production-refresh.json
```

The scheduled refresh records transfer bytes, URL, HTTP status, duration, fallback
use, and per-source coverage in `reports/production-crawl.json`. It fails closed when
there is neither a successful observation nor a readable stored fallback.

## GitHub Pages playground

The `pages.yml` workflow builds the static index playground and deploys it with
the GitHub Pages artifact/deploy actions. It runs after relevant pushes to
`main`, after registry crawls, and when a changed dictionary is published.
During the build it snapshots
the latest `artifact-data` crawl report into `status.json`, including the next
scheduled crawl time, source cursors, failures, and coverage kind.

The browser client reads canonical metadata and executable/index shards from
the public `dictionary` JSON tree. It implements the read-only query surface for
exact checks, batch checks, provider reads, bounded prefix/scope searches,
similar-name searches, coverage, and freshness assessment. No query payload is
sent to an application server. The repository Pages setting must use
**GitHub Actions** as its source the first time the workflow is enabled.

```sh
python tools/refresh.py fixtures/intermediate/*.jsonl \
  --root /tmp/global-executables-fixture \
  --snapshot 2026-08-14 --coverage-kind partial \
  --report /tmp/global-executables-fixture/refresh.json
```

It writes the requested refresh report, refuses missing collector inputs, and
validates the generated tree. This local command does not publish to GitHub;
the scheduled `refresh.yml` workflow runs the production equivalent and
publishes the complete generated replacement to `dictionary`. A source using stored evidence
is recorded as a fallback with unknown current coverage; a source with no usable
fallback fails and cannot be represented as successful coverage.

Real-content smoke test (network, substantial downloads, and Cargo required):

```sh
pytest -m live -o addopts= -q
python tools/live_smoke.py --output reports/upstream-smoke.json
```

Local MCP (no network):

```sh
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
global-executables-mcp --root . --dataset-root .dictionary
```

Remote Streamable HTTP:

```sh
global-executables-mcp --root . --dataset-root .dictionary \
  --transport streamable-http --host 0.0.0.0 --port 8000
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
