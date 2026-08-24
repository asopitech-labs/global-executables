# Published dictionary branch

## At a glance

The repository has two intentionally separate lifecycles:

| Ref | Owns | Written by |
| --- | --- | --- |
| `main` | program, schemas, workflows, fixtures, and documentation | reviewed source changes |
| `dictionary` | canonical executable records, derived indexes, metadata, history, and production refresh reports | `refresh.yml` only |

`dictionary` is an orphan branch. A source checkout therefore avoids the tens
of thousands of generated files and case-conflicting paths in the published
tree. A refresh restores the preceding dictionary before rebuilding, validates
the complete result, and replaces only the generated branch. A failed refresh
leaves the previously published dictionary intact.

## Stable raw URLs

Use this base URL for published data:

```text
https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary
```

For example, metadata is available at
`https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary/data/metadata.json`.

The former `.../main/data/...` URLs stopped being supported at the deliberate
cutover that removed generated dictionary paths from `main`. Raw GitHub URLs
cannot redirect across branches; consumers must change the ref to `dictionary`.

## Local MCP layout

Keep program and data roots explicit:

```sh
git clone https://github.com/asopitech-labs/global-executables.git
cd global-executables
git fetch origin dictionary
git worktree add .dictionary origin/dictionary
python -m pip install -e .
global-executables-mcp --root . --dataset-root .dictionary
```

`--root` supplies program-owned schemas. `--dataset-root` supplies the
published `data/` tree. The environment equivalents are
`GLOBAL_EXECUTABLES_ROOT` and `GLOBAL_EXECUTABLES_DATASET_ROOT`.

The unresolved case-insensitive filename collision is tracked separately. Until
that fix lands, materialize the dictionary on a case-sensitive filesystem.

## Publication and rollback

`refresh.yml` is the sole writer. It fetches `origin/dictionary`, restores its
data to the build workspace so first-seen history and shrink protection remain
effective, then publishes a validated full replacement. The workflow dispatches
the Pages deployment only after a changed dictionary commit is pushed.

Rollback is a branch-only operation: move `dictionary` back to a previously
validated dictionary commit. No source commit on `main` needs to be reverted,
and a source rollback must not move the published data ref.

If the ref is deleted, restore it from a known validated dictionary commit
(the initial seed is `c05ebc42b8d19d341c6ade54a82cb73ecfd9cc4f`) before
re-enabling refresh, validation, freshness, or Pages workflows. The workflows
intentionally fail closed instead of synthesizing an empty replacement branch.
