# Issue #84: publisher notification lifecycle (design, not implemented)

Status: design for review. No publisher intake, verification, or automatic publication is live. A submitted issue must not be described as an indexed provider.

## Goal and first delivery boundary

Accept a small, versioned release notification through a GitHub Issue form or manifest PR. Return a durable receipt, independently verify the public package and installed command, and feed verified observations into the **normal** dictionary publication path. The first supported evidence sources should be npm package tarballs and PyPI wheels. A notification is a discovery lead, never a reservation of an executable name, proof of publisher control, or an instruction to run a submitted installation command.

The first delivery is complete only when an author can follow one release from submission to a named dictionary snapshot and see the provider in both MCP and Pages. A form or validator alone is not a working feature.

## Durable data and identities

Use three separate records:

1. An append-only receipt/event ledger in the durable `artifact-data` branch: immutable manifest digest, receipt ID (GitHub issue node ID and revision), submitter, receive time, event kind, verification attempts and evidence. Public issue comments expose the same status in human-readable form; they are not the source of truth.
2. A source-owned current-state projection in `artifact-data` for each verified publisher-discovered provider. Normal refresh restores it alongside other observations, rebuilds the dictionary, and retains it across later crawls. Directly editing `dictionary` would be erased by refresh.
3. The generated canonical dictionary, with provider evidence and a reference to the receipt and verification result. The event ledger stays separate from lookup data. Pending or rejected submissions never appear as verified providers.

Define a stable claim identity from canonical ecosystem, canonical package ID, executable name, and target constraint. Version, source URL, receipt ID and repository URL are observations, not identity. Keep the registry's package identity and the submitter's identity distinct. An issue author can suggest any public package, but cannot assert control merely by posting an issue. Ownership-sensitive rename, deprecation or removal requires independent control evidence or maintainer review. Package/entry-point verification remains mandatory even if ownership is proven.

The current `provider_key` includes version and source, and `pipeline.merge()` copies only a fixed set of fields. Before ingestion, define a provenance schema and merge rule that coalesces crawler and publisher observations of the same claim without dropping either source's evidence or unrelated same-name providers. Coverage stays additive and partial.

## State machine and acknowledgment

`received → validating → needs_information | rejected | verified_pending_publish → indexed`

An accepted receipt is written before verification starts. `needs_information` and `rejected` include a machine-readable reason and evidence links; a corrected manifest produces a new revision/event under the same issue. `verified_pending_publish` records the verified evidence and queues normal refresh. `indexed` is set **only after** the published dictionary snapshot is fetched and the expected provider/receipt is found in it. Publication failure, timeout or conflict leaves the event pending for reconciliation; it never reports success early. A newer accepted event may mark an older event `superseded`, while retaining its history. The public issue exposes the receipt ID, current state, reason, and snapshot/link when indexed.

Deduplicate by canonical manifest digest plus claim/event identity, not by issue number alone. Retries of an event return the same outcome. Two issues for the same claim/release link to the same verified evidence; concurrent deliveries cannot duplicate providers. Reconciliation must repair divergence between the ledger, issue status, source projection and dictionary after interrupted jobs.

## Verification and event rules

For `publish` or `update`, first validate a bounded manifest: schema version, ecosystem, exact package and version, command(s), declared entry-point path, authoritative release URL and optional target/recipe fields. Retrieve authoritative registry metadata for that exact version, verify the artifact digest and bounded archive contents, and confirm that the declared command is an installed entry point. A repository name or any matching file inside an archive is insufficient. Record each independent result: release exists, artifact integrity, command entry point, target applicability, and publisher control. Missing or conflicting evidence yields `needs_information` or `rejected`, with a reason. Evidence should have a check time and be revalidated when freshness policy expires.

Do not compare arbitrary version strings lexically or assume SemVer. Keep verified release events; select the current observation using registry-specific publication metadata and a documented rule. A delayed old notification cannot replace newer verified evidence. An unpublished, yanked or withdrawn version cannot remain a current verified release solely because it was once observed.

`rename`, `deprecate` and `remove` are explicit events. A rename adds a newly verified entry point and separately retires the old publisher-source claim only when current public evidence supports that retirement; it does not erase a crawler-observed command. A deprecation preserves discovery with a warning and provenance. A removal needs authoritative withdrawal/entry-point absence evidence or manual review, writes a tombstone in the publisher source, and passes the dictionary shrink guard with an auditable reason. Omitting a row is not removal: the existing partial-source merge preserves omitted rows. A submitter cannot delete another package's provider or any same-name provider.

Submitted install instructions are unverified hints. #83's resolver may return `ready` only after its own target-specific install-and-invoke evidence matches; publisher provenance alone never upgrades a recipe.

## GitHub workflow and publication boundary

The Issue/PR intake handler accepts only bounded JSON and treats all text and URLs as untrusted data. The verification job has read-only permissions, fixed registry host allowlists, time/size limits and no dynamic shell evaluation of manifest fields. A separate limited writer records a verified source event with a compare-and-swap update to the durable branch and requests normal refresh. Issue-writing permission is used solely to acknowledge state. Static Pages contains no write credential. A scheduled reconciler handles failed verification/publication, issue edits and interrupted runs.

The normal refresh must restore the publisher source, merge it without overwriting other sources, validate generated data, publish `dictionary`, and trigger Pages. A post-publication check reads the resulting snapshot and MCP/Pages data before marking the receipt `indexed`. Preserve source ownership in concurrent publishes; retry against a fresh branch head rather than force-pushing. Rollback disables intake and reverts only the publisher source's current projection, leaving the event ledger and other providers intact.

## Tests required before enabling intake

1. Schema and evidence fixtures: malformed/oversized input, missing release, mismatched package/version/digest, absent or broken entry point, unsupported target, and untrusted repository claims.
2. State transitions: durable acknowledgment, needs-information correction, rejection, verification, failed publication, retry/reconciliation, and indexed status only after reading the published snapshot.
3. Replay/concurrency: duplicate issue events, equivalent manifests across issues, concurrent updates and delayed old releases; compare-and-swap conflicts do not lose observations.
4. Lifecycle: new release, version update, entry-point rename, deprecation, withdrawal/removal, and reappearance; preserve historical receipts and prevent stale-version rewind.
5. Integration: publisher and crawler observations for one package coalesce with both provenances; unrelated same-name providers remain; partial coverage and existing MCP lookups do not change. A fixture follows submission through `artifact-data` projection, normal refresh, canonical dictionary, MCP, Pages, and #83's `unverified`/`ready` boundary.
6. A real disposable npm and PyPI release fixture or pinned public package verifies archive metadata, installable entry point, and resulting snapshot. The first production submission is observed through the same checks before claiming the feature is operational.

## Design decisions to settle before implementation

- Which exact existing `artifact-data` source layout and publish lock the new source will use, and how the receipt ledger is made atomic with its current-state projection.
- The provider/provenance schema migration and stable claim-key normalization for npm scopes, PyPI normalized names, aliases and target variants.
- The evidence required to treat a GitHub submitter as a package controller. Until defined, the UI must label submissions as public discovery suggestions, with control unverified.
- The per-ecosystem current-release and withdrawal policy, including prereleases and yanked versions.
- Whether removal is always reviewed by a maintainer in the first release. Automatic third-party removal must not be enabled by default.
