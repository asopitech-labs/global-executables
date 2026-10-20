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

The agent should keep rejected names internal:

1. Generate candidate names internally.
2. Call `check_executables` once for the batch.
3. Keep the explicit `found` observation separate from absence confidence;
   `unknown` means insufficient coverage, not an unavailable query.
4. Call `search_similar_executables` for not-found candidates.
5. Remove confusing names and present only survivors, with snapshot/coverage caveats.

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
