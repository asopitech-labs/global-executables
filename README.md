# Global Executables

**A public cross-ecosystem dataset of executable names, continuously collected from package ecosystems and exposed to AI agents via MCP.**

`global-executables` builds and maintains a machine-readable index of executable names found across software package ecosystems.

It is designed to answer a deceptively simple question:

> **Is this executable name already in use?**

This is especially useful when naming new CLI tools. Searching package names, repositories, or the web is not sufficient because a package name and the executable it installs are not necessarily the same.

Global Executables collects executable-level information, publishes it as structured data on GitHub, and makes the dataset directly accessible to AI agents through MCP.

---

## Why Global Executables?

Suppose you are designing a new CLI and considering:

```text
envc
evcr
evpk
envcp
```

Before recommending or adopting one of these names, you want to know whether it is already installed as a command by an existing package.

The relevant relationship is:

```text
executable
    ↓
provider
    ↓
package
    ↓
ecosystem
```

rather than simply:

```text
package name
```

Global Executables creates this missing cross-ecosystem index.

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
        └── others
                │
                ▼
          GitHub Actions
                │
       collect / normalize
                │
                ▼
        Structured dataset
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

The intended repository structure is:

```text
global-executables/
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
│   └── metadata.json
│
├── collectors/
│   ├── debian/
│   ├── ubuntu/
│   ├── arch/
│   ├── homebrew/
│   ├── npm/
│   ├── pypi/
│   └── crates/
│
├── schema/
├── mcp/
└── .github/
    └── workflows/
```

The canonical dataset uses ordinary structured files rather than requiring a database server.

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
| npm | `bin` metadata |
| PyPI | distribution entry points / `console_scripts` |
| crates.io | Rust binary targets |
| others | ecosystem-specific executable metadata |

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

## MCP

Global Executables is intended to be directly usable by AI agents.

The MCP interface provides structured access to the public dataset.

Expected tools include:

### `check_executable`

Check one proposed executable name.

```json
{
  "name": "envc"
}
```

### `check_executables`

Check multiple candidates in one request.

```json
{
  "names": [
    "envc",
    "evcr",
    "evpk",
    "envcp"
  ]
}
```

### `get_executable`

Return providers and provenance for a known executable.

### `search_executables`

Search by prefix, length, ecosystem, or other indexed attributes.

### `search_similar_executables`

Find potentially confusing existing names using lexical similarity.

### `get_coverage`

Return dataset sources, freshness, and current coverage.

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
 Remove known collisions
            │
            ▼
 search_similar_executables
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

Results should therefore distinguish between:

```text
collision
clear_in_index
unknown
```

For example:

```json
{
  "name": "evpk",
  "status": "clear_in_index",
  "checked_sources": [
    "debian",
    "arch",
    "homebrew",
    "npm",
    "pypi",
    "crates"
  ],
  "snapshot": "2026-08-14"
}
```

`clear_in_index` means that no collision was found within the stated dataset coverage.

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

Global Executables deliberately keeps its canonical data in the repository.

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

The schemas, deterministic builder, derived prefix/length/ecosystem/trigram
indexes, fixture-driven parser prototypes, read-only local/HTTP MCP server,
and CI checks are implemented in this repository. Parser prototypes are not
described as production collectors until full-crawl work is completed. See
[`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) for exact naming, sharding, alias, and
history rules and [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for reproducible
commands and honest measured coverage limitations.

Install and query a checked-out snapshot without network access:

```sh
python -m pip install -e .
global-executables-mcp --root .
```

Use `--transport streamable-http` for remote access. Both modes expose
`check_executable`, `check_executables`, `get_executable`,
`search_executables`, `search_similar_executables`, and `get_coverage`; neither
mode exposes writes. Data is reusable under the repository license by reading
the canonical JSON directly, without running MCP.
See [`docs/MCP.md`](docs/MCP.md) for local client configuration, the remote
endpoint, resources, health visibility, and the survivor-only agent workflow.

The scheduled job is currently an upstream-content smoke check, not a dataset
refresh. Coverage is snapshot-specific and reported by `get_coverage`. The
checked-in initial snapshot is a deterministic test corpus;
it is **not** full ecosystem coverage, so consumers must preserve
`clear_in_index`/`unknown` semantics and the accompanying coverage caveat.

Successful collection does not by itself permit a negative collision claim.
Fixture, smoke, and partial snapshots return `unknown` for absent names;
`clear_in_index` requires metadata explicitly marking the complete queried
snapshot exhaustive. Derived search indexes are integrity-pinned in metadata
and are used directly by prefix, length, ecosystem, and similarity queries.
