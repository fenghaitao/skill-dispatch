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
   - `herdr`: create a background tab, start a named agent, wait for
     readiness, then submit the task.

   `--agent` picks the worker CLI: `claude` (Claude Code, the default),
   `copilot` (GitHub Copilot CLI) or `codex` (OpenAI Codex CLI). Copilot and
   Codex work with the `local` and `herdr` backends; see
   [GitHub Copilot workers](#github-copilot-workers) and
   [OpenAI Codex workers](#openai-codex-workers).

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
AGENT=copilot scripts/demo.sh local /tmp/sd-copilot --accept-edits \
  --allow-tool 'shell(python3)'
AGENT=copilot scripts/demo.sh herdr /tmp/sd-copilot-herdr --accept-edits \
  --allow-tool 'shell(python3)'
AGENT=codex scripts/demo.sh local /tmp/sd-codex --accept-edits
AGENT=codex scripts/demo.sh herdr /tmp/sd-codex-herdr --accept-edits

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
Copilot and Codex workers get the equivalent settings described under
[GitHub Copilot workers](#github-copilot-workers) and
[OpenAI Codex workers](#openai-codex-workers).

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
`HERDR_WORKSPACE_ID` available, plus the worker CLI (`claude`, `copilot` or `codex`):

```bash
python3 orchestrator/skill_dispatch.py dispatch --root workspaces \
  --task "Summarize sales.csv" --backend herdr --accept-edits --ready 120
```

Successful Herdr workers remain open so their output/history can be inspected
with Herdr's agent/session commands.

## Herdr launch check

Herdr types the worker command into the tab's interactive shell, so a shell alias
or wrapper can add options, for example `alias copilot='copilot --allow-all'`.
Before submitting, dispatch reads the pane's foreground processes with
`herdr pane process-info` and requires the worker to run with exactly the
requested options. Otherwise it reports what it found and leaves the tab open
without sending the task. Remove or rename such an alias to use the backend.
The check was validated with Herdr 0.9.3 (protocol 22) for Claude, Copilot and
Codex workers.

Herdr's prompt gate requires the worker to start within five seconds of
accepting input. A worker can be slower, for example Copilot reloading MCP
servers after a trust prompt. After `agent_prompt_stalled`, dispatch keeps
observing for up to 60 seconds without resubmitting and accepts `working`, or
`done` with an advanced `state_change_seq`.

## GitHub Copilot workers

`--agent copilot` starts [GitHub Copilot CLI](https://docs.github.com/copilot)
instead of Claude Code. Copilot loads project skills from `.claude/skills/`
as well as `.github/skills/`, so the workspaces need no changes. Copilot must be
installed and signed in (`copilot login`).

- `local` runs `copilot -p PROMPT -s` in the folder with stdin disabled.
- `herdr` starts the worker with `herdr agent start --kind copilot` and then
  follows the same readiness, launch-check and submission steps as Claude.
- `orca` rejects `--agent copilot` for now.

Every Copilot worker gets `--no-ask-user` and no blanket approvals: under `-p`,
Copilot denies writes and commands that no rule allows, and an interactive
worker prompts for them. The permission options map as follows:

| Option | Claude Code | Copilot CLI |
| --- | --- | --- |
| default | `--permission-mode dontAsk` | no `--allow-tool` rules |
| `--accept-edits` | `--permission-mode acceptEdits` | `--allow-tool=write`, `shell(mkdir)`, `shell(mv)` |
| `--allow-tool RULE` | `--allowedTools=RULE,...` | one `--allow-tool=RULE` per rule |

`shell(mkdir)` and `shell(mv)` approximate the common filesystem commands that
Claude's `acceptEdits` allows. Copilot approves `shell(...)` rules by command
name, not by path, so these rules are not a filesystem sandbox.
Rules use the agent's own syntax. Copilot rules such as `shell(python3)`,
`shell(git:*)` or `url(github.com)` are passed through unchanged. Claude rules
such as `Bash(...)` or `Edit` are rejected for Copilot before anything starts.
Copilot matches shell rules on the command name, not the full command line:
`shell(python3)` approves every `python3` command, which is broader than
`Bash(python3 -m pytest -q)`. Use `--model` to pick a Copilot model.

The dispatcher controls only Copilot's command line. The `COPILOT_ALLOW_ALL`
environment variable and the `defaultPermissionMode` and `defaultMode` settings
in `~/.copilot/settings.json` can still widen approvals, and the Herdr launch
check cannot see them. Leave them unset for conservative workers.

## OpenAI Codex workers

`--agent codex` starts [OpenAI Codex CLI](https://github.com/openai/codex).
Codex does not discover `.claude/skills/`, so the prompt for a Codex worker
also names the selected skill file, for example
`.claude/skills/summarize-csv/SKILL.md`. Discovery already knows that path, so
the dispatcher still reads only frontmatter and the workspaces need no changes.
The printed plan shows the exact prompt. Codex must be installed and signed in
(`codex login`), and the workspace must be a Git repository.

- `local` runs `codex exec --sandbox MODE -- PROMPT`; `codex exec` never asks
  for approval.
- `herdr` starts the worker with `herdr agent start --kind codex` and adds
  `--ask-for-approval never`, so Codex returns failures to the model instead of
  prompting. It also adds `--no-daemon` and
  `--config=check_for_update_on_startup=false`: with a conflicting shared Codex
  server, Codex showed a settings modal that Herdr reported as idle and ready,
  and the update dialog's default option runs an installer.
- `orca` rejects `--agent codex` for now.

Codex permissions are a sandbox mode rather than per-tool rules. The sandbox is
always passed explicitly, so `~/.codex/config.toml` or a profile cannot pick it:

| Option | Claude Code | Codex CLI |
| --- | --- | --- |
| default | `--permission-mode dontAsk` | `--sandbox read-only` |
| `--accept-edits` | `--permission-mode acceptEdits` | `--sandbox workspace-write` |
| `--allow-tool RULE` | `--allowedTools=RULE,...` | rejected before anything starts |

`workspace-write` lets Codex run any command without asking, inside an OS
sandbox that allows writes to the workspace. That
is broader than Claude's `acceptEdits` in what may run, but writes are
confined: in a live check, writing to the home directory failed with
"Read-only file system". Use `--model` to pick a Codex model.

The dispatcher controls only Codex's command line. Execpolicy `.rules` files
(for example in `~/.codex/rules`) and other Codex configuration can still
change what runs, and the Herdr launch check cannot see them. Codex readiness
relies on Herdr's detection, which misreported one modal as ready before
`--no-daemon` was added.

Codex asks whether to trust each new folder and saves the answer in
`~/.codex/config.toml`. The dispatcher never answers that prompt.

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
arguments for Claude, Copilot and Codex, Orca/Herdr readiness and trust behavior,
the Herdr launch check, backend acknowledgements, and the demo's
scratch-repository safety. Backend tests use mocks; they do not prove that a
live Claude/Copilot/Codex/Orca/Herdr installation will complete a task correctly.
