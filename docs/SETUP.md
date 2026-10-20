# End-to-end MCP setup and verification

This guide verifies the read-only service from a clean checkout. The examples
use the Python MCP client SDK `mcp==1.29.0`, the version used by the protocol
tests in this repository.

## Local stdio

```sh
git clone https://github.com/asopitech-labs/global-executables.git
cd global-executables
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
global-executables-mcp --root .
```

Configure an MCP client with:

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

Call `check_executables` with `{"names":["envcp","evpk"]}`. The fixture
snapshot returns `collision` for `envcp` and `unknown` plus
`found:false`/`not_found_in_current_index` for `evpk`.

## Streamable HTTP

```sh
global-executables-mcp --root . --transport streamable-http \
  --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

Configure the client endpoint as `http://127.0.0.1:8000/mcp`. The health
response includes service version, snapshot, coverage scope, and `read_only`.

## Agent verification workflow

The tested workflow is: generate candidates privately, call
`check_executables`, retain `found:false` candidates while preserving their
absence confidence, call `search_similar_executables`, then show survivors
with snapshot and coverage caveats. For richer evidence, call
`assess_executables`; it never turns a stale provider into `found:false`.

Troubleshooting: a missing `data/metadata.json` means `--root` is wrong; a
transport mismatch means the client endpoint and server transport differ; and
fixture/partial coverage intentionally produces `unknown`, not
`clear_in_index`.
