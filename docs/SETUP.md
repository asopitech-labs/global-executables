# End-to-end MCP setup and verification

This guide verifies the read-only service from a clean checkout. The examples
use the Python MCP client SDK `mcp==1.29.0`, the version used by the protocol
tests in this repository.

Developing the transactional Go registry crawler has a separate, container-first
toolchain and build contract. A host Go installation is optional; see
[`GO_CRAWLER_DEVELOPMENT.md`](GO_CRAWLER_DEVELOPMENT.md) before changing crawler
code.

## Local stdio

For the Codex-specific setup and verification flow, see
[`CODEX_MCP_TUTORIAL.md`](CODEX_MCP_TUTORIAL.md).

```sh
git clone https://github.com/asopitech-labs/global-executables.git
cd global-executables
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
global-executables-mcp --root . --dataset-root .dictionary
```

Configure an MCP client with:

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

Call `check_executables` with `{"names":["envcp","evpk"]}`. The published
2026-08-15 snapshot returns `collision`/`found:true` for `envcp`. For `evpk`,
it returns `found:false`, `status:"unknown"`, and
`absence.status:"not_found_in_current_index"` with
`absence.confidence:"insufficient_coverage"`; this is expected because the
snapshot has partial registry coverage.

To reproduce the small deterministic fixture separately, build it into a
temporary root and point the server at that root:

```sh
global-executables build fixtures/intermediate/*.jsonl \
  --root /tmp/global-executables-fixture \
  --snapshot 2026-08-14 --coverage-kind fixture
global-executables-mcp --root . \
  --dataset-root /tmp/global-executables-fixture
```

## Streamable HTTP

```sh
global-executables-mcp --root . --dataset-root .dictionary \
  --transport streamable-http \
  --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

Configure the client endpoint as `http://127.0.0.1:8000/mcp`. The health
response includes service version, snapshot, coverage scope, and `read_only`.

## Container Codex smoke test

The repository includes a reproducible container test. It builds the MCP
server, starts it on an isolated Docker network, registers it in Codex inside
a separate test container, and calls the MCP health endpoint, tools, and
resources:

```sh
tools/test_container_mcp.sh
```

The test image pins the Codex CLI with `CODEX_VERSION` (default `0.147.0`). It
only verifies MCP registration and protocol behavior; it does not make a
model/API request or require an OpenAI login.

## Agent verification workflow

The tested workflow is: generate candidates privately, call
`check_executables`, retain `found:false` candidates while preserving their
absence confidence, call `search_similar_executables`, then show survivors
with snapshot and coverage caveats. For richer evidence, call
`assess_executables`; it never turns a stale provider into `found:false`.

Troubleshooting: a missing `data/metadata.json` means `--dataset-root` does not
point at a materialized `dictionary` branch; a missing schema means `--root`
does not point at the program checkout. A
transport mismatch means the client endpoint and server transport differ; and
fixture/partial coverage intentionally produces `unknown`, not
`clear_in_index`.

## Freshness report

The `get_coverage` MCP tool and `global-executables://coverage` resource also
expose an incremental freshness report when `reports/freshness.json` is
materialized in the dataset root. The `dictionary` branch does not copy the
separately published `freshness-data` report automatically, so the default is
`unavailable`. When supplied, it describes the partitions checked by the latest bounded run;
unvisited or failed partitions remain stale/unknown and do not change the
negative-lookup contract.
