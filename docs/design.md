# Design

## Goal

`skill-dispatch` is a thin control plane for routing a task to a skill that
lives inside one of several Git-managed workspaces.

The intended production layout is a parent repository whose child workspaces
are Git submodules. Each workspace owns its normal Claude skills and remains
usable without `skill-dispatch`.

## Non-goals

The dispatcher is not:

- a skill execution engine;
- a durable run-history database;
- a task-completion verifier;
- a replacement for backend session logs;
- a protocol that existing skills must implement.

In particular, skills must not need receipt instructions, tokens, synthetic
dispatch IDs, or other dispatcher-specific content.

## Flow

```text
task
  |
  v
discover skill frontmatter
  |
  v
route to workspace + skill
  |
  v
capture Git identity
  |   repo_commit + skill path
  v
start worker in workspace cwd
  |
  v
backend process/session owns execution lifecycle and history
```

### Discovery

For every direct child workspace, scan:

```text
.claude/skills/*/SKILL.md
```

Read only YAML frontmatter needed for routing:

```yaml
name: ...
description: ...
```

The orchestrator does not interpret skill bodies. Claude reads the selected
skill normally after it starts in that workspace.

### Routing

If `--skill` is supplied, use that exact globally unique skill name.
Otherwise rank skills by word overlap between the task and
`name + description`.

No match or a tie fails closed instead of guessing.

### Git identity

Before starting a worker:

1. resolve the workspace's Git repository;
2. require the selected `SKILL.md` to be tracked;
3. require that skill file to have no staged or unstaged changes;
4. record `HEAD`;
5. record the repository-relative path of the selected `SKILL.md`.

The submodule commit plus that path identifies the exact skill contents selected for the run.

Other files in the workspace may be dirty. The worker is expected to modify
workspace files as part of the task.

For a Git-submodule deployment, the submodule pointer in the parent repository
provides an additional natural record of the workspace revision.

### Execution identity and history

Do not create a separate `dispatch_id` or `run_id`.

The execution already has a backend-native identity:

- local: one synchronous subprocess invocation;
- Orca: terminal/worktree handle;
- Herdr: workspace/tab/pane/agent identity.

Those backend objects and their logs are the authoritative source for execution
history, prompts, activity, output, and status.

Git identity and execution identity answer different questions:

```text
Git:      what workspace/skill snapshot was selected?
Session:  what happened when the agent ran?
```

A synthetic UUID adds no independent information and should not be introduced
unless a future backend lacks any usable execution identity and a concrete
correlation problem requires one.

### Completion and verification

There is no generic receipt or `verify` command.

A worker-written receipt only proves that the worker followed an instruction to
write a receipt; it does not establish task correctness. Backend session state
already provides better lifecycle information.

Task correctness is domain-specific and belongs to the task/skill itself:
tests, builds, generated artifacts, human review, or other acceptance checks.

## Backends

### print

Return the routing plan and Git identity without starting an agent.

### local

Run `claude -p` with the workspace as cwd. Process exit and captured output are
the lifecycle result.

### Orca

Create/use the workspace worktree, start Claude in an Orca terminal, wait for
interactive readiness, then send the task. Do not answer trust prompts.

The returned terminal handle is the execution identity.

### Herdr

Create a background tab in the inherited Herdr workspace, start a named Claude
agent, wait for interactive readiness, then submit the task.

The returned workspace/tab/pane/agent fields are the execution identity and can
be used to inspect the session later.

## Permissions

Routing to a workspace does not grant permissions.

Workers start conservatively with `--permission-mode dontAsk`. Wider edit or
tool permissions require explicit CLI opt-ins. A cwd is not a security
boundary; use OS/container isolation where needed.

## Design principle

Keep the dispatcher responsible only for:

```text
discover -> route -> capture provenance -> launch
```

Everything else should stay with the system that already owns it:

- skill instructions: the workspace;
- source/version identity: Git;
- execution/session identity: the backend;
- task correctness: task-specific validation.
