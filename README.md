# skill-dispatch

A small prototype that discovers skills in several Git-managed workspaces,
routes a task to the best matching skill, and starts an agent in that workspace.

The dispatcher stays deliberately small:

- discovery and routing use only `SKILL.md` frontmatter;
- existing skills are never modified;
- Git identifies the workspace and skill snapshot;
- the backend process/session identifies the execution and provides its history.

See [docs/design.md](docs/design.md) for the architecture.

```
skill-dispatch/
  orchestrator/skill_dispatch.py   discover, route, dispatch
  workspaces/                      three example workspaces
    calc-lib/.claude/skills/add-calc-function/
    release-notes/.claude/skills/write-release-note/
    data-tools/.claude/skills/summarize-csv/
  scripts/demo.sh                  copy examples into fresh Git repos and dispatch
  tests/                           discovery, routing, Git identity and backend tests
```

## How it works

1. **Discover.** Scan each direct child workspace for
   `.claude/skills/*/SKILL.md` and read only the YAML frontmatter
   (`name`, `description`). Skill bodies are not interpreted by the
   orchestrator.
2. **Route.** Choose an explicitly named skill with `--skill`, or use simple
   word overlap between the task and each skill's name/description. A tie or
   no match fails closed and asks for an explicit skill.
3. **Capture Git identity.** Before dispatch, require the selected
   `SKILL.md` to be tracked and clean. Record the workspace repository commit
   and the repository-relative skill path. This naturally fits the intended
   deployment where each workspace is a Git submodule.
4. **Dispatch.** Start the worker with the selected workspace as its cwd and
   tell it to use the selected skill.
   - `print`: show the plan only;
   - `local`: run `claude -p` synchronously;
   - `orca`: create an Orca terminal, wait for Claude readiness, then send the task;
   - `herdr`: create a background tab, start a named Claude agent, wait for
     readiness, then submit the task.

There is no dispatcher receipt, synthetic run ID, or verify command. Execution
state and history belong to the backend session/process. Git identity answers
which skill snapshot was selected; the session answers what happened during the
run.

## Usage

This project supports Python 3.13 and uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev

uv run python orchestrator/skill_dispatch.py discover --root workspaces
uv run python orchestrator/skill_dispatch.py route --root workspaces \
  --task "Summarize sales.csv"
uv run python orchestrator/skill_dispatch.py dispatch --root workspaces \
  --task "Summarize sales.csv" --backend print

source .venv/bin/activate
scripts/demo.sh print /tmp/sd-plan
scripts/demo.sh local /tmp/sd-local --accept-edits \
  --allow-tool 'Bash(python3 -m pytest -q)'
scripts/demo.sh orca /tmp/sd-orca --accept-edits \
  --allow-tool 'Bash(python3 -m pytest -q)'
scripts/demo.sh herdr /tmp/sd-herdr --accept-edits \
  --allow-tool 'Bash(python3 -m pytest -q)'

uv run pytest -q tests
```

`demo.sh` copies the three example workspaces into a fresh scratch directory
and initializes each one as its own Git repository. It refuses an existing
destination or one that overlaps this repository. The real deployment can use
Git submodules instead of these scratch repositories.

A print plan includes the selected folder, skill, score, prompt, repository
root, repository commit, and repository-relative skill path.

## Git identity

For a selected skill, dispatch records approximately:

```json
{
  "folder": "/repo/workspaces/calc-lib",
  "skill": "add-calc-function",
  "repo_commit": "<git HEAD>",
  "skill_repo_path": ".claude/skills/add-calc-function/SKILL.md"
}
```

The selected `SKILL.md` must have no staged or unstaged changes when the
worker starts. Other workspace files may already be dirty or may be changed by
the worker. The restriction exists only so `repo_commit + skill_repo_path`
accurately identifies the instructions Claude will see.

No later verify step is required. If a caller wants provenance, it should store
this plan next to the backend's session metadata or rely on the backend's own
session log.

## Execution identity

The dispatcher does not invent another execution ID:

- **local:** the subprocess invocation, exit status and captured output define
  the execution;
- **Orca:** the returned worktree/terminal handle identifies the interactive
  execution;
- **Herdr:** the returned workspace/tab/pane/agent identifiers identify the
  execution and can be used to inspect its session history.

This avoids duplicating lifecycle/history information that the backend already
owns.

## Permissions

All worker backends start Claude with `--permission-mode dontAsk` by default.
Operations that need approval are denied unless existing rules allow them.

Use `--accept-edits` to opt into `acceptEdits`. Use repeatable
`--allow-tool RULE` options for specific tool rules, for example:

```bash
--allow-tool 'Bash(python3 -m pytest -q)'
```

A cwd is not a filesystem sandbox. Use container/OS isolation when a worker
must not access other paths. The dispatcher never answers a trust prompt.

## Orca and Herdr readiness

`--ready SECONDS` controls how long Orca or Herdr may wait for the worker to
become interactively ready. The dispatcher does not answer trust prompts.

Orca returns the selected worktree and terminal handle. Herdr returns the
workspace, tab, pane and named agent. Herdr also requires observed activity
after prompt submission, which confirms that the backend accepted and started
processing the turn; it is not a claim that the requested task is correct.

Run the Herdr backend from a Herdr-managed terminal with `HERDR_ENV=1` and
`HERDR_WORKSPACE_ID` available:

```bash
python3 orchestrator/skill_dispatch.py dispatch --root workspaces \
  --task "Summarize sales.csv" --backend herdr --accept-edits --ready 120
```

Successful Herdr workers remain open so their output/history can be inspected
with Herdr's agent/session commands.

## Skill visibility

A parent-folder Claude session does not initially load child workspace skills.
Starting the worker inside `calc-lib`, for example, makes
`add-calc-function` locally visible. Claude may later discover nested skills
while working in other folders. See
[Claude's skill discovery documentation](https://code.claude.com/docs/en/skills).

The working directory controls initial skill discovery; it is not a lifetime
isolation boundary.

## Tests

The test suite covers frontmatter safety, routing, Git identity, permission
arguments, Orca/Herdr readiness and trust behavior, backend acknowledgements,
and the demo's scratch-repository safety. Backend tests use mocks; they do not
prove that a live Claude/Orca/Herdr installation will complete a task correctly.
