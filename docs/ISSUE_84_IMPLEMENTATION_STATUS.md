# Issue #84 implementation and deployment boundary

The code in this branch wires successful online npm/PyPI discovery from CLI and
MCP to a central HTTPS reservation API. The API independently rechecks the
canonical package and acknowledges only a committed GitHub Contents API update
on `artifact-data`. The scheduled collector leases due reservations, rechecks
the registry, commits source-owned observations to `artifact-data`, and starts
the normal dictionary refresh. Processed packages become due for revalidation
again after 24 hours so removals can be observed without another user query.

This is **not operational merely by merging this code**. An operator must deploy
`python -m global_executables.reservation_server` behind HTTPS with a rate-limited
reverse proxy. The service needs `GLOBAL_EXECUTABLES_GITHUB_TOKEN` with narrowly
scoped Contents write access to this repository's `artifact-data` branch. The
public CLI and MCP server need `GLOBAL_EXECUTABLES_RESERVATION_URL` set to the
service's `/v1/reservations` URL; they must not receive the GitHub token. An
unset URL or failed reservation appears as `reservation.status=unavailable`
beside the live verification result. It does not promise later publication.

The service accepts only `{ecosystem, package}` for npm or PyPI; caller-provided
evidence is ignored. It verifies the selected current release independently.
The request ID is derived from its release/version artifact evidence, so repeated
queries of the same release share a queue item. The central collector uses a
GitHub-backed lease and bounded retry. Source events are preserved by the
existing on-demand publication path; the generated dictionary is marked indexed
only after matching snapshot read-back by the existing reconciler.

Before closing #84, deploy the endpoint and prove one actual CLI and one MCP
query return a durable reservation ID. Confirm that `artifact-data` holds the
reservation, a scheduled Actions run commits the independently checked source
claim, normal refresh publishes it in `dictionary`, and read-back reconciles
the indexed state. Exercise a queue outage and an authoritative withdrawal in
the deployed path. Fixture tests in this branch do not establish those facts.

The current collector handles at most 50 reservations every 15 minutes. Monitor
the pending count and adjust this budget and source rate limits if the queue
outgrows that capacity. A trusted HTTPS proxy must impose per-client limits
before exposing the intake API publicly.
