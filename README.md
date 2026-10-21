![Global Executables checks whether a CLI command name is already taken by indexing executable names across package ecosystems](https://raw.githubusercontent.com/asopitech-labs/global-executables/main/.github/assets/readme-cover.webp)

<h1 align="center">Global Executables</h1>

<p align="center">
  <strong>Check whether a CLI command name is already in use—before you publish it.</strong>
  <br>
  Search executable names across npm, PyPI, crates.io, Go modules, Homebrew,
  Debian, and more from your AI agent or browser.
</p>

<p align="center">
  <a href="https://asopitech-labs.github.io/global-executables/"><strong>Try the Playground</strong></a>
  ·
  <a href="#quick-start"><strong>Install the MCP server</strong></a>
  ·
  <a href="docs/MCP.md"><strong>Read the docs</strong></a>
  ·
  <a href="https://github.com/asopitech-labs/global-executables/tree/dictionary"><strong>Browse the data</strong></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/global-executables/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/global-executables?logo=pypi&logoColor=white"></a>
  <a href="https://pypi.org/project/global-executables/"><img alt="PyPI downloads" src="https://img.shields.io/pypi/dm/global-executables"></a>
  <a href="https://github.com/asopitech-labs/global-executables/actions/workflows/validate.yml"><img alt="Validation" src="https://github.com/asopitech-labs/global-executables/actions/workflows/validate.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="https://glama.ai/mcp/servers/asopitech-labs/global-executables"><img alt="View on Glama" src="https://img.shields.io/badge/Glama-Global_Executables-06B6D4"></a>
</p>

<!-- mcp-name: io.github.asopitech-labs/global-executables -->

## Why this exists

A package name is not necessarily the command it installs. Searching registries for
package names can miss a collision such as a differently named package that already
ships your proposed command.

| Ordinary package search | Global Executables |
|---|---|
| Searches package and repository names | Searches the actual installed command name |
| Leaves evidence scattered across registries | Normalizes providers across ecosystems |
| A missing result can look deceptively final | Reports coverage and absence confidence |
| Requires a person to repeat the work | Gives AI agents a read-only batch workflow |

The index preserves the relationship that matters:

```text
command → provider → package → ecosystem → source evidence
```

## Try it now—no installation

Open the **[Index Playground](https://asopitech-labs.github.io/global-executables/)**
to check a name, find similar commands, inspect providers, or review source coverage.
The Playground reads the same public JSON dataset as the MCP server, and query data is
not sent to a third-party API.

For an AI-assisted check, connect the MCP server below and ask:

```text
I am naming a new CLI. Check `envc`, `evcr`, `evpk`, and `envcp` for exact
collisions and confusingly similar commands. Keep rejected names internal and
explain the coverage caveat for every survivor.
```

The agent can reject `envcp` with evidence instead of relying on a package-name
search: the command is provided on PyPI by both `chaperone` and `envcp`.

## What you get

| Capability | What it gives you |
|---|---|
| Exact and batch checks | Collision results for one name or a candidate set |
| Similar-name search | Typo and confusion candidates before you publish |
| Provider provenance | Package, ecosystem, version, source, and evidence confidence |
| Coverage-aware negatives | An explicit distinction between “not found” and “proven free” |
| Installation resolution | Conservative install and invoke steps for a target environment |
| Public, inspectable data | Versioned JSON, schemas, freshness, and coverage on GitHub |

The server has no write tools and never installs or executes the software it finds.

## Quick start

[![Install in Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=global-executables&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJnbG9iYWwtZXhlY3V0YWJsZXMiLCJtY3AiXX0%3D)

### 1. Install `uv`

The recommended setup uses [`uv`](https://docs.astral.sh/uv/getting-started/installation/).
You do not need to clone this repository, download the dataset, or create an API key.

Verify that `uvx` is available:

```sh
uvx --version
```

### 2. Add the MCP server

Choose your client below. The first launch downloads the package from PyPI; later
launches reuse `uv`'s cache.

<details open>
<summary><strong>Claude Desktop and Cursor</strong></summary>

Add this to `claude_desktop_config.json`, `~/.cursor/mcp.json`, or a project's
`.cursor/mcp.json`:

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

</details>

<details>
<summary><strong>Claude Code</strong></summary>

```sh
claude mcp add global-executables -- uvx global-executables mcp
```

</details>

<details>
<summary><strong>Codex CLI</strong></summary>

```sh
codex mcp add global-executables -- uvx global-executables mcp
```

Start a new Codex session after adding the server. See the
[Codex setup tutorial](docs/CODEX_MCP_TUTORIAL.md) for registration checks and a
source-checkout setup.

</details>

<details>
<summary><strong>VS Code</strong></summary>

Add this to `.vscode/mcp.json`:

```json
{
  "servers": {
    "global-executables": {
      "type": "stdio",
      "command": "uvx",
      "args": ["global-executables", "mcp"]
    }
  }
}
```

</details>

<details>
<summary><strong>Docker (any stdio MCP client)</strong></summary>

Use this server entry instead of the `uvx` entry:

```json
{
  "command": "docker",
  "args": ["run", "-i", "--rm", "ghcr.io/asopitech-labs/global-executables-mcp:latest"]
}
```

The image is public and contains the server, but not a stale copy of the dataset.

</details>

### 3. Ask your agent

Try one of these prompts:

```text
Is the command name `envc` already taken? Check similar names too.
```

```text
Check `envc`, `evcr`, `evpk`, and `envcp` as possible CLI names. Do not show
rejected candidates; explain the dataset coverage for the survivors.
```

```text
Which package provides the `ruff` executable, and how should I install it in a
Python 3.12 project virtual environment?
```

The agent will normally call `check_executables` for a batch and
`search_similar_executables` for the candidates that remain.

> [!IMPORTANT]
> `found: true` means a collision. `found: false` means only that the current
> snapshot contains no match. When `status` is `unknown`, partial source coverage
> means the name is **not proven to be available**. Read `warnings`,
> `absence.confidence`, and `coverage_scope` before adopting a name.

## How it works

The server is read-only. It fetches only the public `dictionary` branch files a
query needs, caches them in the current user's cache directory, and revalidates
them with `ETag`. Cached files remain usable when the network is temporarily
unavailable.

| Requirement | Details |
|---|---|
| Authentication | None |
| Local dataset | Not required |
| Default transport | stdio |
| Data access | On-demand HTTPS from the public `dictionary` branch |
| Writes or command execution | None; every MCP tool is read-only |
| Supported Python | 3.11+ (handled automatically by `uvx`) |

| Variable | Effect |
|---|---|
| `GLOBAL_EXECUTABLES_DATASET_ROOT` | use a local checkout of the `dictionary` branch instead of HTTPS |
| `GLOBAL_EXECUTABLES_CACHE_DIR` | cache location (default `~/.cache/global-executables`, `~/Library/Caches/global-executables`, or `%LOCALAPPDATA%\global-executables\Cache`) |
| `GLOBAL_EXECUTABLES_OFFLINE=1` | serve only cached files, never touch the network |

`uvx global-executables mcp --help` lists the same options as flags. The package is
published to PyPI from release 1.3.0. See the [MCP guide](docs/MCP.md) for HTTP
transport, offline operation, and running from a source checkout.

## MCP tools at a glance

| Tool | Use it when you want to… |
|---|---|
| `check_executable` | check one exact, case-sensitive command name |
| `check_executables` | check several naming candidates in one call (recommended) |
| `get_executable` | retrieve providers and provenance for a known command |
| `search_executables` | search by prefix, length, ecosystem, or scope |
| `search_similar_executables` | find names that may be confused with a candidate |
| `get_coverage` | inspect source coverage, freshness, and warnings |
| `assess_executable` / `assess_executables` | add freshness, activity, popularity, and collision-risk signals |
| `resolve_installation` / `resolve_installations` | derive environment-specific install and invoke guidance |
| `discover_published_package` | verify commands declared by one current npm or PyPI release |

All tools are read-only. `discover_published_package` makes bounded live registry and
artifact requests but never executes package code or mutates the published dictionary.
Installation resolvers return instructions; they never install or execute software.
Full inputs and result contracts are documented in the [MCP guide](docs/MCP.md).

Maintainers can promote a verified current-release observation through the serialized
`Publish on-demand package observation` workflow. The workflow stores source evidence on
`artifact-data`; the regular refresh remains the only writer of the dictionary branch.

## Supported ecosystems

| Family | Sources | Typical executable evidence |
|---|---|---|
| System packages | Debian, Ubuntu, Arch Linux | Installed package file lists |
| macOS packages | Homebrew | Formula metadata and package contents |
| Language registries | npm, PyPI, crates.io, Go modules, RubyGems, Packagist | Declared bins, entry points, or executable targets |
| C and C++ recipes | ConanCenter, vcpkg, xmake-repo | Built file lists, tool declarations, or marked binary packages |

Every provider keeps its source and a confidence level (`direct`, `filesystem`, or
`inferred`). Coverage differs by ecosystem, so negative results carry explicit
coverage metadata instead of pretending that every registry is exhaustive. See
[Data Sources](#data-sources) for collection details and
[coverage and freshness](#coverage-and-freshness) for the result contract.

## Documentation map

| If you want to… | Start here |
|---|---|
| Connect an MCP client or use HTTP transport | [MCP access and agent integration](docs/MCP.md) |
| Configure Codex step by step | [Codex MCP tutorial](docs/CODEX_MCP_TUTORIAL.md) |
| Verify a clean source checkout | [Setup and verification](docs/SETUP.md) |
| Understand records, indexes, and schemas | [Data model](docs/DATA_MODEL.md) |
| Review collection scope and attribution | [Data sources](docs/DATA_SOURCES.md) |
| Operate or contribute to the data pipeline | [Operations](docs/OPERATIONS.md) |

## Troubleshooting

<details>
<summary><strong>The client says <code>uvx</code> was not found</strong></summary>

Install `uv`, confirm that `uvx --version` works in a new terminal, and restart the
MCP client so it receives the updated `PATH`. If the client uses a restricted
environment, set the configuration's `command` to the absolute path reported by
`command -v uvx` (macOS/Linux) or `where uvx` (Windows).

</details>

<details>
<summary><strong>The tools do not appear in my client</strong></summary>

Restart the client or begin a new agent session after changing its MCP configuration.
Then check the client logs for JSON syntax errors or a failed `uvx` launch. Codex users
can run `codex mcp list` and `codex mcp get global-executables`; Claude Code users can
run `claude mcp list`.

</details>

<details>
<summary><strong>A query fails while offline</strong></summary>

Offline mode can serve only files that earlier queries placed in the cache. Reconnect
once and run the needed queries, or point `GLOBAL_EXECUTABLES_DATASET_ROOT` at a local
checkout of the `dictionary` branch. See [local stdio setup](docs/MCP.md#local-stdio-over-a-checkout).

</details>

<details>
<summary><strong>A missing command returns <code>status: "unknown"</code></strong></summary>

That is expected whenever the relevant ecosystems are not exhaustively covered. It
means “not found in this snapshot,” not “safe to claim.” Use `get_coverage`, check
similar names, and perform additional registry and web searches before publishing.

</details>

---

## Goals

Global Executables aims to provide:

- a public dataset of real-world executable names;
- cross-ecosystem coverage;
- traceable provenance for every collected executable;
- reproducible dataset generation;
- Git-friendly structured data;
- machine-readable indexes for efficient lookup;
- historical tracking of executable names;
- MCP access for AI agents;
- explicit dataset coverage and freshness information.

The dataset itself is the primary artifact.

The MCP server is an access layer over that public data, not the source of truth.

Try the live [Index Playground](https://asopitech-labs.github.io/global-executables/)
to inspect crawl progress and run read-only `check`, `search`, `similar`,
`coverage`, and `assess` queries directly in a browser. The playground is a
static GitHub Pages client over the public JSON dataset; no query data is sent
to a third-party API.

The program and published data have independent lifecycles. Program source,
schemas, tests, and documentation live on `main`; the generated JSON dictionary
lives on the orphan `dictionary` branch. The stable raw-data base URL is:

```text
https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary
```

See [the dictionary branch migration note](docs/DICTIONARY_BRANCH.md) for the
branch contract, local setup, and the `main` URL cutover.

Go development follows the
[JetBrains Modern Go Guidelines](https://github.com/JetBrains/go-modern-guidelines).
See the [Go crawler development and build contract](docs/GO_CRAWLER_DEVELOPMENT.md)
for the versioned review, build, test, security, and release workflow.

---

## Architecture

```text
Package ecosystems
        │
        ├── Debian / Ubuntu
        ├── Arch Linux
        ├── Homebrew
        ├── npm
        ├── PyPI
        ├── crates.io
        ├── Go modules
        ├── RubyGems
        ├── Packagist / Composer
        ├── ConanCenter
        ├── vcpkg
        ├── xmake-repo
        └── others
                │
                ▼
          GitHub Actions (`main`)
                │
       collect / normalize
                │
                ▼
  `dictionary` branch dataset
                │
        ┌───────┴────────┐
        │                │
        ▼                ▼
   GitHub / raw        MCP Server
      data                │
                          ▼
                     AI agents
```

GitHub Actions periodically collects upstream package information, normalizes executable records, regenerates indexes, validates the dataset, and publishes the resulting files.

---

## Data Model

The canonical unit is an **executable name**.

Each executable is represented by a structured file containing its known providers.

Conceptually:

```json
{
  "command": "example",
  "providers": [
    {
      "ecosystem": "example-ecosystem",
      "package": "example-package",
      "version": "1.0.0",
      "repository": "https://example.com/repository",
      "source": "example-source",
      "confidence": "direct"
    }
  ],
  "first_seen": "2026-08-14",
  "last_seen": "2026-08-14"
}
```

A single executable may have multiple providers across multiple ecosystems.

---

## Repository Layout

The `dictionary` branch contains only published artifacts:

```text
dictionary/
├── data/
│   ├── executables/
│   │   ├── en/
│   │   │   ├── envc.json
│   │   │   └── envcp.json
│   │   └── gi/
│   │       └── git.json
│   │
│   ├── indexes/
│   │   ├── prefix/
│   │   ├── length/
│   │   ├── ecosystem/
│   │   └── trigram/
│   │
│   ├── metadata.json
│   └── history.json
└── reports/
    ├── production-crawl.json
    └── production-refresh.json
```

The `main` branch separately keeps `fixtures/`, `schema/`, `src/`, `tools/`,
`tests/`, `docs/`, `playground/`, and `.github/workflows/`; it does not track
the generated paths above. The canonical dataset uses ordinary structured
files rather than requiring a database server.

Derived indexes are reproducible and may be regenerated from the canonical executable records.

---

## Lookup

Exact lookup maps directly to the repository structure.

For example:

```text
envcp
  ↓
prefix: en
  ↓
data/executables/en/envcp.json
```

This allows consumers to retrieve individual records without downloading or querying a centralized database.

Derived indexes support broader queries such as:

- prefix;
- executable length;
- ecosystem;
- similar names.

---

## Data Sources

Collectors are independent for each ecosystem.

Potential sources include:

| Ecosystem | Executable evidence |
|---|---|
| Debian / Ubuntu | package file contents |
| Arch Linux | package files database |
| Homebrew | formula/package contents |
| npm | ecosyste.ms critical population; npm registry `bin` metadata |
| PyPI | distribution entry points / `console_scripts` |
| crates.io | Rust binary targets |
| Go modules | `package main` directories in module archives |
| RubyGems | gemspec `executables` metadata |
| Packagist / Composer | `composer.json` `bin` declarations in Packagist metadata |
| ConanCenter | `bin/` contents of the built package, read from `conanmanifest.txt` |
| vcpkg | `vcpkg_copy_tools(TOOL_NAMES ...)` declarations in `portfile.cmake` |
| xmake-repo | `set_kind("binary")` packages, whose command name is inferred |
| others | ecosystem-specific executable metadata |

C and C++ have no single registry, so three recipe repositories are read instead, and
each contributes the strongest evidence it actually carries. vcpkg names the tools a
port installs, because naming them is what installs them. ConanCenter recipes never
name their executables, so the built package's own file list is read. xmake declares
that a package is a binary but never what the binary is called, so its records are
marked `inferred` and no xmake record should be read as a declaration. None of the
three is `exhaustive`: a vcpkg port can install a command without declaring it, a
ConanCenter recipe nobody has built states nothing about installed files, and an xmake
package can install a command under another name.

The project records how each executable was discovered rather than treating all observations as equally authoritative.

---

## Confidence

Records may carry a confidence level describing how an executable name was established.

For example:

```text
direct
filesystem
inferred
```

**direct** means the executable is explicitly declared by package metadata.

**filesystem** means it was observed in package contents as an installed executable.

**inferred** means it was derived indirectly and should be treated with lower confidence.

Consumers can use this information when deciding whether a potential naming collision is significant.

---

## Installation guidance

`resolve_installation` and `resolve_installations` provide environment-specific
installation guidance. Agents can call these MCP tools directly; people can enter
the same target context in the
[browser Playground](https://asopitech-labs.github.io/global-executables/).
The CLI exposes the resolver against a local dictionary too:

```sh
global-executables resolve-installation pygo --root ./dictionary \
  --environment '{"package_manager":"pip","runtime_version":"3.12","install_scope":"project","virtualenv_bin":"./.venv/bin"}'
```

The [MCP guide](docs/MCP.md) documents the required target context and the
`installation-v1` result contract. Guidance without target-specific
install-and-invoke validation is marked `unverified`. The
[method validation record](docs/INSTALLATION_VALIDATION.md) distinguishes tested
package-manager syntax from untested package identities.

---

## Agent Workflow

A naming agent should use Global Executables **before presenting CLI name recommendations**.

Recommended workflow:

```text
Generate candidate names internally
            │
            ▼
     check_executables
            │
            ▼
 Separate `found:true` collisions from `found:false` observations
            │
            ▼
 search_similar_executables for not-found candidates
            │
            ▼
 Remove confusing candidates
            │
            ▼
 Present surviving names
```

Rejected candidates do not need to be surfaced to the user.

The purpose of the service is to make collision checking part of the agent's normal reasoning workflow rather than a manual follow-up task.

---

## Result Semantics

Absence from the dataset does **not** prove that an executable name has never been used.

Results therefore distinguish the factual observation from confidence in
absence:

```text
collision
clear_in_index
unknown
```

For example:

```json
{
  "name": "evpk",
  "found": false,
  "status": "unknown",
  "snapshot": "2026-10-19",
  "coverage_scope": "unknown",
  "checked_sources": [
    "arch",
    "crates",
    "debian",
    "go",
    "homebrew",
    "npm",
    "pypi",
    "ubuntu"
  ],
  "searched_sources": [
    "arch",
    "crates",
    "debian",
    "go",
    "homebrew",
    "npm",
    "pypi",
    "ubuntu"
  ],
  "coverage": {
    "arch": {
      "status": "success",
      "coverage_kind": "exhaustive",
      "records": 1647,
      "source": "data/production/intermediate/arch.jsonl"
    },
    "npm": {
      "status": "success",
      "coverage_kind": "partial",
      "records": 127,
      "source": "data/production/intermediate/npm.jsonl",
      "scope": "ecosyste.ms critical npm population covering 80% of downloads or dependent repositories",
      "catalog_complete": true,
      "catalog_packages": 2295,
      "catalog_snapshot": "2026-10-18",
      "catalog_source": "https://packages.ecosyste.ms/critical?registry=npmjs.org",
      "catalog_digest": "sha256:4b610ce561d63b72b7a4137ed75ba5ecbec5fbafda236218fc4264c92db605b3"
    }
  },
  "warnings": [
    {
      "code": "npm_coverage_incomplete",
      "ecosystem": "npm",
      "message": "npm coverage is incomplete or limited to a declared population. A missing result is not proof that no npm package uses this executable and is not permission to adopt the name.",
      "coverage": {
        "status": "success",
        "coverage_kind": "partial",
        "source": "data/production/intermediate/npm.jsonl",
        "scope": "ecosyste.ms critical npm population covering 80% of downloads or dependent repositories",
        "catalog_complete": true,
        "catalog_packages": 2295,
        "catalog_snapshot": "2026-10-18",
        "catalog_source": "https://packages.ecosyste.ms/critical?registry=npmjs.org",
        "catalog_digest": "sha256:4b610ce561d63b72b7a4137ed75ba5ecbec5fbafda236218fc4264c92db605b3"
      },
      "reference": "https://github.com/asopitech-labs/global-executables/issues/54"
    }
  ],
  "absence": {
    "status": "not_found_in_current_index",
    "confidence": "insufficient_coverage",
    "searched_sources": [
      "arch",
      "crates",
      "debian",
      "go",
      "homebrew",
      "npm",
      "pypi",
      "ubuntu"
    ]
  }
}
```

`found:false` means that the name was not found in the current index. `unknown`
means the available coverage is insufficient to make an exhaustive absence
claim; it does not mean that the query was unavailable. `clear_in_index` is
returned only when every source in the queried snapshot has
`coverage_kind: exhaustive`, a successful run, and no finite population limit
(`scope` or `catalog_*` fields). A completed npm critical catalog sets
`catalog_complete: true` and keeps `coverage_kind: partial`; it never makes
the snapshot exhaustive. Incomplete npm coverage also adds a
`npm_coverage_incomplete` warning. None of these statuses is permission to
adopt a name.

The `coverage` object reports the per-source collection status, coverage kind,
and record count. The example shows two representative sources; the live
response includes all sources in the snapshot (and may also include each
source's input path).

It does not mean that the name is legally or globally guaranteed to be available.

---

## Coverage and Freshness

Dataset metadata records:

- generation timestamp;
- snapshot identifier;
- number of indexed executables;
- enabled collectors;
- freshness of each upstream source;
- collector status.

This information is available both as structured data and through MCP.

Agents can therefore evaluate the strength of a lookup result instead of treating the index as complete by assumption.

---

## GitHub as the Source of Truth

Global Executables deliberately keeps its canonical data in the repository's
orphan `dictionary` branch.

This provides:

- transparent provenance;
- reviewable changes;
- reproducible generation;
- historical tracking;
- forks and mirrors;
- direct raw-file access;
- independence from the availability of the MCP service.

If the hosted MCP endpoint disappears, the dataset remains usable.

A third party can build another MCP server, search engine, CLI, or analysis tool directly from the repository.

---

## Automation

GitHub Actions maintains the dataset.

The general pipeline is:

```text
scheduled / manual trigger
          │
          ▼
     run collectors
          │
          ▼
       normalize
          │
          ▼
        merge
          │
          ▼
 generate executable files
          │
          ▼
    generate indexes
          │
          ▼
       validate
          │
          ▼
        publish
```

Collectors should contain the actual collection logic independently of GitHub Actions so they can also be executed and tested locally.

`act` may be used during development to run compatible GitHub Actions workflows locally.

---

## What Global Executables Is Not

Global Executables is not:

- a trademark search service;
- a brand-name availability service;
- a package registry;
- a package manager;
- a vulnerability database;
- a software catalog;
- proof that an unused name is legally available.

It answers a narrower question:

> **What executable names are known to be used across the software ecosystems covered by the dataset?**

Trademark, company-name, domain, repository-name, and broader web searches remain separate checks.

---

## Potential Uses

Beyond CLI naming, the dataset can support:

- automated CLI naming assistants;
- repository CI policies;
- package publishing checks;
- ecosystem collision analysis;
- executable discovery;
- historical studies of CLI ecosystems;
- IDE and developer-tool integrations;
- AI-agent preflight checks.

---

## Project Principle

**Public data first. MCP second.**

The value of Global Executables is not tied to a particular server implementation.

The durable asset is the openly available, reproducible, cross-ecosystem executable-name dataset.

MCP makes that dataset immediately useful to agents.

## Current implementation

The schemas, deterministic builder, production OS index collector, derived
prefix/length/ecosystem/trigram and logical-scope indexes, collector parsers,
read-only local/HTTP MCP server, assessment API, bounded fixture freshness
scenarios, and CI checks are implemented in this repository. Coverage is measured
against each source. npm stays `coverage_kind: partial` even when its finite
ecosyste.ms critical catalog finishes (`catalog_complete: true`); the all-registry
npm long tail is not in the index. See
[`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) for exact naming, sharding, alias, and
history rules and [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for reproducible
commands, freshness-state scenarios, and honest measured coverage limitations.

For zero-setup use, see [Quick start](#quick-start-use-it-from-your-ai-agent). To
query a pinned snapshot without network access, install the program, materialize
the published branch in a separate worktree, and point the server at it:

```sh
python -m pip install -e .
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
global-executables-mcp --root . --dataset-root .dictionary
```

Use `--transport streamable-http` for remote access. Both modes expose
`check_executable`, `check_executables`, `get_executable`,
`search_executables`, `search_similar_executables`, `get_coverage`,
`assess_executable`, and `assess_executables`; neither
mode exposes writes. Data is reusable under the repository license by reading
the canonical JSON directly from the `dictionary` branch, without running MCP.
See [`docs/MCP.md`](docs/MCP.md) for local client configuration, the remote
endpoint, resources, health visibility, and the survivor-only agent workflow.

The manual fixture workflow exercises a bounded incremental freshness scan. It
rotates through immutable test partitions, persists a cursor and last successful
observations on the `freshness-data` branch, and publishes a machine-readable test
report. It is not production freshness or a full dataset refresh. Coverage is
snapshot-specific and reported by `get_coverage`. The
published snapshot contains production OS/Homebrew coverage, a completed but
partial npm critical catalog (`catalog_complete`, not registry-wide exhaustiveness),
and partial coverage for other registries such as PyPI and Go; it is **not** full
global ecosystem coverage, so consumers must preserve
`clear_in_index`/`unknown` semantics and the `npm_coverage_incomplete` warning.

Registry artifact inspection is separately resumable. CI refreshes the finite npm
critical catalog and crawls it to 100% daily, while dedicated local containers advance
PyPI and Go module cursors and publish checkpoints to `artifact-data`; these local
containers are temporary completion boosters and stop at 100%. After a source reaches
100%, a six-hour GitHub Actions matrix cycles through every indexed package with a
persisted refresh cursor. Successful results replace stale package observations,
including commands removed by a newer release. CI also performs
one daily crates.io dump change check and downloads the dump only when it changed.
The CI refresher dispatches a dictionary rebuild only when normalized observations
changed and redeploys Pages whenever it publishes crawl progress. Go modules, npm, PyPI, RubyGems, and Packagist use the transactional Go runtime;
their bbolt state commits cursor, retry verdicts, and observations together. CI
regenerates npm's bounded population; PyPI, RubyGems, and Packagist reuse sampled
catalogs, while Go extends its index. Finishing a declared catalog may set
`catalog_complete`; npm is never promoted to `coverage_kind: exhaustive` from that
catalog alone. Other sources become `exhaustive` only after their declared catalog
and required artifact inspections complete successfully without a finite population
limit.

Successful collection does not by itself permit a negative collision claim.
Every valid query returns a factual `found` observation. Fixture, smoke, and
partial snapshots return insufficient absence confidence for absent names;
`clear_in_index` requires metadata explicitly marking the complete queried
snapshot exhaustive. Derived search indexes are integrity-pinned in metadata
and are used directly by prefix, length, ecosystem, and similarity queries.

For a clean-checkout setup, client configuration, expected responses, and
verification steps, see [`docs/SETUP.md`](docs/SETUP.md).

Issue completion and release evidence follow [`docs/RELEASE.md`](docs/RELEASE.md);
implementation claims alone do not close work.

Data sources, their licenses and terms, and required attribution are listed in [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md).
