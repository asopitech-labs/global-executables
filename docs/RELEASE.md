# Release and issue closure policy

Issues are not closed from an implementation claim alone.

For each milestone:

1. Assign every issue to the milestone and link the implementing PR.
2. Verify every acceptance criterion with deterministic tests, CI, and any
   required upstream or agent smoke evidence.
3. Publish a semantic-versioned GitHub release from the verified commit. The
   notes must state the snapshot, commands run, coverage kind, limitations, and
   links to the implementing PRs.
4. Add the release link and verification summary to each completed issue.
5. Close only the completed issues with `state_reason=completed`.
6. Keep the milestone open until every issue assigned to it is completed; an
   incomplete milestone is not a release claim.

Fixture, smoke, and partial snapshots cannot justify `clear_in_index` or an
exhaustive-coverage claim. Failed collectors remain visible in coverage
metadata and block a release that requires exhaustive data.

For an incremental freshness release, the release notes must also link the
freshness report and state the partition budget, record/byte budget, scheduler
cursor, source checksums, selected/skipped/failed partitions, changes,
removals, and staleness. A freshness release is a `partial` observation and
must not be described as an exhaustive registry crawl. The report/state may be
published on the dedicated `freshness-data` branch; canonical executable data
must not be replaced by a failed or incomplete partial scan.
