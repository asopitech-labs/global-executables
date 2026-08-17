# MCP access and agent integration

For a step-by-step Codex CLI setup, see
[`CODEX_MCP_TUTORIAL.md`](CODEX_MCP_TUTORIAL.md).

The server is a read-only view of a checked-out `data/` tree. It does not copy
records into a database and requires no network access in local stdio mode.

## Local stdio

After `pip install .`, use this generic MCP client configuration:

```json
{
  "mcpServers": {
    "global-executables": {
      "command": "global-executables-mcp",
      "args": ["--root", "/absolute/path/to/global-executables"]
    }
  }
}
```

Clients whose configuration uses `type` may additionally require
`"type": "stdio"`. The command and arguments are vendor-neutral.

## Streamable HTTP

```sh
global-executables-mcp --root . --transport streamable-http \
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

`assess_executable` and `assess_executables` add freshness, activity,
popularity, and collision-risk dimensions. They preserve provider evidence and
expose the assessment methodology version; they do not change a stale provider
into a negative existence result.

See [`SETUP.md`](SETUP.md) for a clean-checkout verification guide and the
tested MCP client version.
