# MCP access and agent integration

For a step-by-step Codex CLI setup, see
[`CODEX_MCP_TUTORIAL.md`](CODEX_MCP_TUTORIAL.md).

The server is a read-only view of a materialized `dictionary` branch. Program
schemas and published data have separate roots; the server does not copy records
into a database and requires no network access after both roots are present.

## Zero-setup stdio (recommended)

```sh
uvx global-executables mcp
```

With no local dataset the server reads the published `dictionary` branch from
`https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary`,
fetching only the files a query touches. Files are cached per user
(`GLOBAL_EXECUTABLES_CACHE_DIR`, default `$XDG_CACHE_HOME/global-executables`).
The metadata is revalidated with `If-None-Match` at most every ten minutes, and each
cached file is revalidated with its `ETag` once per published metadata generation, so
an unchanged file costs a `304` without a body. A derived index whose hash does not
match the current metadata is fetched again in full. When the network is unreachable,
cached files are served as they are; `--offline` (or `GLOBAL_EXECUTABLES_OFFLINE=1`)
never touches the network. `--dataset-url` points at another published copy.

Client configuration:

```json
{
  "mcpServers": {
    "global-executables": {
      "command": "uvx",
      "args": ["global-executables", "mcp"]
    }
  }
}
```

The container image `ghcr.io/asopitech-labs/global-executables-mcp` runs the same
stdio server (`docker run -i --rm ghcr.io/asopitech-labs/global-executables-mcp`) and
contains no dataset. Mount a volume at `/cache` to keep its cache between runs.

## Local stdio over a checkout

After `pip install .`, use this generic MCP client configuration:

```sh
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
```

```json
{
  "mcpServers": {
    "global-executables": {
      "command": "global-executables-mcp",
      "args": [
        "--root", "/absolute/path/to/global-executables",
        "--dataset-root", "/absolute/path/to/global-executables/.dictionary"
      ]
    }
  }
}
```

Clients whose configuration uses `type` may additionally require
`"type": "stdio"`. The command and arguments are vendor-neutral.

## Streamable HTTP

```sh
global-executables-mcp --root . --dataset-root .dictionary \
  --transport streamable-http \
  --host 0.0.0.0 --port 8000
curl http://127.0.0.1:8000/health
```

Connect an MCP client to `http://127.0.0.1:8000/mcp`. The health response exposes
the service version, dataset snapshot, coverage scope, and `read_only: true`.

## Resources

* `global-executables://metadata`
* `global-executables://coverage`
* `global-executables://schema/{executable|provider|intermediate|metadata}`
* `global-executables://executables/{name}`

## Naming-agent workflow

### On-demand package discovery

`discover_published_package(ecosystem, package, version?, command?, target?)` checks
one known npm or PyPI package against current authoritative metadata and its exact
release artifact. npm `bin` targets must exist in the tarball; PyPI `console_scripts`
targets must resolve to a module in a target-compatible wheel. The versioned
`package-discovery-v1` response distinguishes `verified_entry_point`, `unverified`,
`not_published`, `temporarily_unavailable`, and `unsupported`. A timeout, throttle, or
5xx response is never converted to an absence claim.

The operation is read-only: it caches bounded evidence locally and reports
`publication_status: not_indexed`; it does not write `artifact-data` or `dictionary`.
An omitted version means the registry-selected current release. CLI and MCP use the
same engine:

```sh
global-executables discover-package npm typescript --version 5.9.3 --command tsc
global-executables discover-package pypi global-executables --command global-executables \
  --target '{"python_tag":"py3","platform_tag":"any"}'
```

Use this operation only when the package identity is already known. It cannot perform
complete command-name reverse search; use the snapshot lookup tools for that purpose.

Maintainer-triggered durable promotion is deliberately separate from the MCP operation.
The `Publish on-demand package observation` workflow performs a fresh, uncached check of
the registry-selected current release only. It writes an append-only observation event,
the package's current claim, and the `on-demand` intermediate projection to
`artifact-data`. The normal refresh replaces only providers owned by that source and
publishes the dictionary. A final read-back records `indexed` on the claim only when the
published provider contains the same claim and event digests; otherwise the claim remains
queued for retry. Exact-version checks remain read-only so a later query for an old
release cannot rewind current durable evidence.

### Environment-specific installation guidance

`resolve_installation(name, environment, preferences?)` and
`resolve_installations(names, environment, preferences?)` return the versioned
`installation-v1` contract. The browser Playground exposes the same operations.
The initial managers are Homebrew on macOS, apt on Debian/Ubuntu, npm, and pip.
The Playground provides target OS, manager, distribution, architecture, runtime,
scope and privilege selectors; advanced JSON can supply additional target facts.
Pass `package_manager`; apt also needs `distribution` (`debian` or `ubuntu`) and
`has_admin: true`, plus `distribution_release` and `architecture`. Homebrew needs
`os: macos`; npm/pip need `runtime_version` and `install_scope` (`user` or
`project`). Project pip requires `virtualenv_bin`, the path to that environment's bin directory. An optional
`preferences.package` selects a particular package when several supply the name.

The result reports `ready`, `needs_context`, `ambiguous`, `unsupported`,
`unverified`, or `not_found_in_index`. A `ready` result contains the exact provider,
ordered `install` and `invoke` argv steps, invocation/PATH guidance, and source,
observation, version, and confidence evidence. These are recipes derived from the
index; the service does not run the command on the target host. Until a recipe has
target-specific install-and-invoke validation, it remains `unverified`, even when
candidate steps are shown. The bounded set of verified recipes and their target
conditions are listed in the [validation record](INSTALLATION_VALIDATION.md).
Other provider/target combinations remain `unverified`. Only direct or
filesystem command evidence is eligible. Aliases and inferred command names are
`unverified`; multiple packages are `ambiguous`. `method.documentation` links the
package manager's installation syntax; `verification` separately reports whether
the selected package is known to be available and runnable on the target. For apt,
missing release provenance adds a `check_availability` step before install.
Missing names retain the index
coverage and absence caveats. Do not treat a first provider as a recommendation.
The response's `method_validation` field reports earlier disposable method probes;
it does not validate the selected provider. See the
[validation record](INSTALLATION_VALIDATION.md) for targets, versions, dates, and
results.

The agent should keep rejected names internal:

1. Generate candidate names internally.
2. Call `check_executables` once for the batch.
3. Keep the explicit `found` observation separate from absence confidence;
   `unknown` means insufficient coverage, not an unavailable query.
4. Call `search_similar_executables` for not-found candidates.
5. Remove confusing names and present only survivors, with snapshot/coverage caveats.

### Negative results and npm coverage warnings

`check_executable` returns an object even when a name is missing. `found: false`
means no matching provider was found in this snapshot. `status: unknown` and
`absence.confidence: insufficient_coverage` mean the search cannot establish absence.
`clear_in_index` is restricted to an explicitly exhaustive, unfiltered snapshot with
successful, unbounded source coverage; it is never permission to adopt a name.

Both single and batch checks include `warnings` (an array, possibly empty). Each
batch item also includes its own warnings. Incomplete, failed, missing, or limited
npm coverage produces `code: npm_coverage_incomplete`, an explanation, the available
scope/catalog provenance, and a link to issue #54. MCP text and structured content
carry the same information. Coverage resources and `get_coverage` expose these
warnings too; assessments preserve them from the underlying check.

A completed 2,295-package npm critical catalog is not complete npm registry coverage.
The builder records `coverage.npm.catalog_complete` separately and keeps its
`coverage_kind: partial`. Query readers also treat legacy `exhaustive` descriptors
containing scope or catalog restrictions as insufficient for negative completeness.
The Playground reports the critical subset as such and keeps its negative-result
coverage warnings.

Provider `scope` is a filter, not a declaration of the searched population. Scoped
negative queries remain `unknown` until coverage can establish completeness for the
requested dimensions; positive matching providers still return `collision`.
Invalid command names (empty, over 255 characters, path separators, control
characters, `.` or `..`) raise input errors, including through MCP and Playground.

The Python/MCP/Playground contract fixtures run in `tests/test_query_contract.py`.
Node.js is required for the browser checks and is installed by the validation job.

There are no write tools or write HTTP routes.

## Browser playground

The [Index Playground](https://asopitech-labs.github.io/global-executables/)
is a static GitHub Pages client for experimenting with the same read-only
operations in a browser. It displays the latest registry crawl report,
scheduled next crawl, source cursors, failures, and coverage status. Queries
are resolved against the public `dictionary` JSON dataset, while crawl status is
published from the `artifact-data` branch after each crawl.

The raw-data base URL is
`https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary`.
The previous `main/data/...` URLs do not redirect; see
[`DICTIONARY_BRANCH.md`](DICTIONARY_BRANCH.md) for the cutover note.

Freshness state is published independently on `freshness-data`. A plain
`dictionary` worktree therefore reports freshness as `unavailable` unless
`reports/freshness.json` is materialized into that dataset root.

`assess_executable` and `assess_executables` add freshness, activity,
popularity, and collision-risk dimensions. They preserve provider evidence and
expose the assessment methodology version; they do not change a stale provider
into a negative existence result.

See [`SETUP.md`](SETUP.md) for a clean-checkout verification guide and the
tested MCP client version.
