# Issue #84: distributed discovery through Git history

Status: revised design for review. The deployed implementation still lets the
central collector contact registries and update claims. The successful
2026-10-21 runner-to-dictionary smoke test proves that implementation's path,
not the distributed lifecycle specified here. Do not close #84 on that test.

## Roles and authority

GitHub holds the shared state. The designated GitHub Actions runners execute
the MCP/CLI agent and make observations of known published packages. A runner
records each completed observation as a Git commit on an `intake/<event-id>`
branch. Runners have the same repository credentials; no runner is a more
authoritative registry observer than another. There is no intake API, separate
database, publisher submission form, or branch-scoped credential requirement.

The central scheduled collection job **only merges history**. It does not
contact a registry, revalidate an observation, select a current release,
rewrite a claim, publish a dictionary row, or delete an intake ref. It fetches
pending event branches and creates merge commits on `artifact-data`. A separate
refresh job derives a materialized view from merged events and publishes
`dictionary`. Git commit ancestry, not collector discretion, is the durable
record of what was observed and what was merged.

| Actor | Input | Git effect |
| --- | --- | --- |
| MCP/CLI or periodic observation runner | Authoritative registry metadata and a bounded release artifact | Create one immutable event branch and read back its ref before ACK. |
| Scheduled collector | Existing Git event branches | Merge their commits into `artifact-data`; on a non-fast-forward, fetch and retry. |
| Refresh/publisher | Events reachable from `artifact-data` | Derive the current source view, publish `dictionary`, and record read-back publication status. |

The reviewed Actions workflow code is trusted to follow this protocol. The
shared `GITHUB_TOKEN` is repository-scoped; no claim of a branch-specific ACL
is made. Registry responses and branch payloads are data, not executable
instructions. The collector can check a candidate's commit shape and path as
a mechanical merge precondition, but cannot improve an observation by doing
the same network request again.

## Observation events and reservation

Before fetching a package, the runner reads the current `artifact-data` head.
That SHA becomes the event branch's parent and records the Git history visible
when the observation began. The runner fetches official registry metadata and
an exact, bounded artifact, then records the complete MCP/CLI result and its
evidence URLs, digest, checked time, selector (`latest` or exact version),
target, outcome, and verifier contract. A later observation of the same
artifact is a new event: its ID hashes the canonical event body **including
the acquisition time**. Only byte-identical events deduplicate.

An event commit adds exactly one bounded JSON file at
`data/production/on-demand/events/<event-id>.json`. Its branch is
`intake/<event-id>`. The runner uses an empty expected-ref lease to create
the branch, then fetches the ref and checks its commit and payload before
returning `reservation.status=reserved`. A concurrent identical request or
lost push response is resolved by ref read-back. A different payload at the
same ref is a conflict; the runner never overwrites it. If Git is unavailable,
the live MCP/CLI answer remains usable but reports reservation failure.

Only completed, authoritative observations become source events. An explicit
`not_published` package result or `verified_no_entry_point` result can be a
withdrawal event when the adapter proves that absence from official
metadata/artifact evidence. The existing ambiguous `unverified` outcome
cannot withdraw a provider.
Timeout, 429, 5xx, and ambiguous metadata are not withdrawal events. They are
reported to the caller and can be retried by a runner. An exact-version query
is historical evidence; only a complete, target-neutral `latest` observation
participates in the current published view. A command-only reverse search
still uses the partial dictionary; it cannot scan all registries live.

## Merge-only collection

The collector enumerates a bounded set of `intake/*` refs. For each candidate,
it checks that the tip is a single commit whose parent is an `artifact-data`
ancestor and whose tree delta is exactly the correctly named, bounded event
file. These are Git merge preconditions, not a second package verification.
It skips a tip already reachable from `artifact-data`. For any other valid
tip, it merges with `--no-ff`, so the event commit remains a merge parent, and
pushes the new `artifact-data` head without force. If another writer moves
`artifact-data`, it fetches that head and attempts the same merge again.

Distinct event paths make ordinary concurrent merges conflict-free. A
malformed branch or unexpected tree conflict remains unmerged and visible as
an intake ref; the collector reports a failed item but continues the bounded
batch. It neither invents a semantic winner nor silently discards the ref.
Correctness does not depend on deleting merged refs: reachability identifies
completed work. Any later retention cleanup belongs to a separate job and
must delete only a ref whose fetched SHA is still merged, using a ref lease.

## Materializing current information

The refresh job folds only events reachable from `artifact-data`; an intake
branch alone never changes the dictionary. It retains every event in history.
An exact release does not replace a current claim. A latest observation can
update the current claim, while an authoritative absence can withdraw only
the on-demand provider. Unrelated crawler evidence and same-name providers
remain intact. A successful verification is not a #83 target-specific
install-and-invoke `ready` result.

The fold is deterministic from the event DAG. If one latest event's base
includes another accepted event, it is causally later. An older event merged
afterward cannot rewind that later observation. Equivalent concurrent events
coalesce while retaining both histories. Incompatible concurrent events have
no Git causal order, so commit merge order and wall-clock time alone do not
declare one to be current. The refresh preserves the last unambiguous
published provider, records the conflict as unresolved, and queues another
designated runner observation from a head that includes both events. The
refresh can dispatch the existing GitHub Actions runner for this; it does
not perform the observation itself. That
later observation resolves the conflict if it supplies authoritative evidence.
The checked time remains visible for freshness and TTL, but is not a
substitute for Git causality. Clock anomalies and equal-time conflicting
evidence are unresolved, not silently ordered.

A periodic GitHub Actions **observation runner**, separate from the merge-only
collector, selects aged active or unresolved claims from Git state and issues
the same MCP/CLI acquisition. It writes new event branches even when the
package and artifact digest are unchanged, so evidence can become fresh.
Withdrawal and reappearance use these runner events. The first version
selects a bounded batch by oldest Git-recorded acquisition time; no external
cursor is needed. The collector never performs the check. A delayed Actions schedule affects
freshness, not the safety of earlier observations.

The refresh first publishes the derived source projection through the normal
dictionary build. It marks an event `indexed(snapshot)` only after reading
back a dictionary provider with the matching event digest. Dispatch success
or an `artifact-data` merge is merely pending publication. If publication
fails, the merged event remains in Git and the next refresh can retry. Pages
deployment follows a successful dictionary publication.

## Review and migration gates

The current implementation does **not** satisfy this design. In particular,
`git_intake_collector.py` calls the discovery engine and sweeps current claims;
`git_intake.py` derives an ID without acquisition time and only reserves
verified command-bearing outcomes; `on_demand.py` updates the current claim
outside a fold over merged event history. These paths must change together.
Removing the collector's network call alone would lose freshness and
withdrawal handling. The migration must preserve the already indexed legacy
on-demand provider until the new event fold has a causally usable observation;
legacy claim/event files stay as read-only history during that transition.

Before closing #84, tests must show: two equal-authority runners creating
concurrent event branches; duplicate and lost-ACK handling; no overwrite of a
moved ref; a collector that merges without registry traffic; non-fast-forward
retry; exact-history-only behavior; repeated same-artifact observations;
authoritative withdrawal, temporary failure, and reappearance; conflicting
concurrent latest observations and a resolving later event; unchanged other
providers; failed refresh followed by matching dictionary read-back. Then
repeat a production GitHub Actions runner → merge-only collector → refresh →
dictionary/Pages smoke test. The earlier smoke test remains evidence for the
old implementation, not a substitute for this gate.
