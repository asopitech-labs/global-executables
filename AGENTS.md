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
