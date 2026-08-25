# Agent communication contract

## Always name the actor

Every agent must state the grammatical subject in plans, progress updates, decision
rationales, review findings, collaboration messages, and final reports. Japanese
messages must not rely on an omitted subject when the actor can affect how the reader
interprets responsibility or evidence.

Use explicit forms such as:

- `私は ... を確認します` for an action performed by the active agent.
- `テストは ... を検証しました` for evidence produced by a test.
- `CI は ... で失敗しました` for an external system result.
- `Claude Code は ... を指摘しました` for an external reviewer finding.
- `実装担当エージェントは ...、レビュー担当エージェントは ...` when agents
  collaborate.

Every delegation must name the delegating agent, the executing agent, and the expected
reviewer. Every handoff must distinguish what the executing agent reported from what
the primary agent independently verified. A report must never use a subjectless phrase
such as `確認済み`, `修正しました`, or `問題なし` when it leaves the responsible
actor ambiguous.

This rule applies to user-visible summaries of reasoning and decisions; it does not
require disclosure of private chain-of-thought. Agents must provide concise rationale,
evidence, assumptions, and responsibility boundaries with explicit subjects instead.

## Modern Go development

Before an agent edits a Go file, the agent must read the complete JetBrains Modern Go
Guidelines list for that file and the Go version declared by `go.mod`:

```console
./tools/go_container.sh go run github.com/JetBrains/go-modern-guidelines@v0.1.1 list --file-path internal/gocrawl/coordinator.go
```

The agent must not truncate or filter the list. When a returned guideline may apply
but its effect is unclear, the agent must run the same command with `explain` and the
specific guideline IDs. The agent must apply relevant guidance unless it changes
behavior or does not compile for the declared Go version.

The shared pipeline enforces the standard-library modernization fixes. The agent must
run `./tools/go_container.sh ./tools/go_pipeline.sh modern` while editing and the full
`check` command before handoff. A host Go installation is not required.
