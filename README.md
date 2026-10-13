# skill-dispatch

A small prototype of an orchestrator that discovers the skills of several
folders and dispatches work to an agent in the right folder. The worker finds
the skill in its own folder. Discovery and routing parse only frontmatter.
Dispatch records the selected skill's committed Git identity without interpreting
its body, so existing skills need no skill-dispatch-specific instructions.

```
skill-dispatch/
  orchestrator/skill_dispatch.py   discover, route, dispatch, verify
  workspaces/                      three folders, each with its own skill
    calc-lib/.claude/skills/add-calc-function/
    release-notes/.claude/skills/write-release-note/
    data-tools/.claude/skills/summarize-csv/
  scripts/demo.sh                  copy the workspaces into sub-repos and run
  tests/                           discovery, routing and receipt checks
```

## How it works

1. **Discover.** Scan each folder for `.claude/skills/*/SKILL.md` and read only
   the frontmatter (`name`, `description`) into a catalog. The skill body is
   never parsed here. YAML frontmatter is limited to 16 KiB, parsed safely, and
   duplicate keys are rejected. Names must be lowercase letters or digits in
   words separated by hyphens, at most 64 characters. Receipt paths must stay
   inside the workspace's `.skill-receipts/` directory, including through symlinks.
2. **Route.** Pick the skill for a task: the one named with `--skill`, or the
   best word overlap between the task and the skills' names and descriptions.
   No match, or a tie, asks for `--skill`.
3. **Dispatch.** Require the selected `SKILL.md` to be tracked and clean in Git,
   record the current repository commit and that file's Git blob ID, then start a
   worker with the chosen folder as its working directory and tell it which skill to use:
   - `print`: show the plan only;
   - `local`: `claude -p` as a subprocess in the folder;
   - `orca`: register the folder with Orca, open a terminal there, start
     `claude`, wait until it is ready, then send the task;
   - `herdr`: create a background tab in the caller's Herdr workspace with
     the selected folder as its cwd, start a named Claude worker through
     `herdr agent start`, then submit the task with `herdr agent prompt`.
4. **Verify.** The dispatcher prompt, not the skill, tells the worker to atomically
   write `.skill-receipts/<skill>.json` with the skill name, absolute working
   directory, dispatch ID, repository commit and selected skill blob. The
   `verify` command checks that the recorded commit contains that blob and that
   the current `SKILL.md` still matches the same committed version. It requires
   `--dispatch-id ID` and fails for missing, invalid, stale or mismatched receipts.
   It checks every discovered skill by default; repeat `--skill NAME` to require
   only the skills dispatched in that run. Receipts provide evidence that the
   current dispatch reached its completion marker; they do not prove that the
   requested work is correct or complete.

## Usage

This project supports Python 3.13 only and uses [uv](https://docs.astral.sh/uv/) to manage the environment and dependencies.

Create or update the local environment with:

```bash
uv sync --group dev
```

Run the dispatcher and tests through uv so they use the managed Python 3.13 environment:

```bash
uv run python orchestrator/skill_dispatch.py discover --root workspaces
uv run python orchestrator/skill_dispatch.py route --root workspaces --task "Summarize sales.csv"
uv run python orchestrator/skill_dispatch.py dispatch --root <dir> --task "..." --backend local --dispatch-id example-run
uv run python orchestrator/skill_dispatch.py verify --root <dir> --dispatch-id example-run --skill <selected-skill>

source .venv/bin/activate
scripts/demo.sh print /tmp/sd-plan        # destination must not exist
scripts/demo.sh local /tmp/sd-local --accept-edits --allow-tool 'Bash(python3 -m pytest -q)'
scripts/demo.sh orca /tmp/sd-orca --accept-edits --allow-tool 'Bash(python3 -m pytest -q)'
scripts/demo.sh herdr /tmp/sd-herdr --accept-edits --allow-tool 'Bash(python3 -m pytest -q)'
uv run pytest -q tests
```

`demo.sh` copies each workspace into a fresh scratch folder as its own git
repository (a sub-repo). It rejects destinations that exist or overlap this
repository, including aliases through symlinks. It never deletes an existing
destination. Omit the scratch argument to create a new temporary folder.
It validates the backend and permission options before creating the scratch
folder. Print mode produces plans and explicitly skips receipt verification;
worker modes verify the three expected receipts with one fresh run ID.
Scratch copying leaves the original repository alone; worker access to other
folders still depends on permissions and external isolation.

## Permissions

All worker backends explicitly start Claude with `--permission-mode dontAsk`.
Configured permission rules still apply; operations needing approval are denied
unless an existing rule allows them. Dispatch does not add blanket tool approvals.
The default can therefore prevent a worker from editing files or writing a receipt.

Pass `--accept-edits` to opt in to `acceptEdits` for in-scope file edits and common
filesystem commands. Pass repeatable `--allow-tool RULE` options to preapprove
specific tools, for example `--allow-tool 'Bash(python3 -m pytest -q)'`.
Bare tool names such as `--allow-tool Bash` grant broad approvals and should be
chosen deliberately. A working directory is not a filesystem sandbox; use a
container or other isolation when workers must not access other folders.
The dispatcher never answers a trust prompt, including with either opt-in flag.

## Git identity, dispatch IDs and waiting

Each dispatch records two different kinds of identity. `repo_commit` plus
`skill_blob` identify the committed skill definition. `dispatch_id` identifies
one execution attempt. They are intentionally not interchangeable: many tasks can
run against the same pinned submodule commit and the same skill blob.

Dispatch generates a fresh ID if `--dispatch-id` is omitted and includes it in
the JSON plan and worker prompt. Use a unique ID for each run; never reuse an ID
for a different task in the same workspace. Record the generated ID, or supply
your own unique ID when coordinating dispatch and verification. Receipts without
the Git identity fields or dispatch ID fail validation.

The selected skill must be tracked and have no staged or unstaged changes when
dispatch starts. This is a narrow consistency check on `SKILL.md`; other task
files may be dirty or may change while the worker runs. Git submodules fit this
model naturally because the submodule checkout already provides a pinned commit.

Local dispatch always checks for a complete receipt for the current dispatch
after the synchronous worker returns. A zero worker exit with a missing or
invalid receipt makes dispatch exit nonzero. `--wait` allows extra waiting for
a successful worker's receipt; a failed worker does not trigger extra waiting.

`--wait SECONDS` waits for complete receipt JSON with the selected skill, absolute
folder, current dispatch ID, and the exact commit/blob identity captured before
launch. Waiting does not parse skill bodies; run `verify` afterward to confirm
that Git still resolves the receipt to the same skill content. An Orca send acknowledgement
proves input acceptance, not a started turn. Herdr additionally requires observed
worker activity after submission. Orca delivery metadata and warnings and Herdr
prompt responses are retained in the output. Readiness and receipt wait
durations must be finite and nonnegative. `--ready` applies to Orca and Herdr
startup and never answers a prompt.
Each Orca wait subprocess gets a five-second transport margin to return its
blocked reason. Results arriving after the readiness deadline never permit
task submission, and readiness errors preserve the last known blocked reason.

## Herdr

Run `--backend herdr` from a Herdr-managed terminal. It requires `HERDR_ENV=1`
and `HERDR_WORKSPACE_ID`, plus `herdr` and `claude` in the terminal environment.
The backend uses the inherited Herdr session and explicitly targets the caller's
workspace, preserving focus with `--no-focus`. Each dispatch creates a new tab
and a unique worker name; the JSON result includes the workspace, tab, pane,
and agent identifiers. It uses Herdr's JSON responses directly, without Orca's
`--json` flag. The interface was validated with Herdr 0.8.2 (protocol 20).

```bash
python3 orchestrator/skill_dispatch.py dispatch --root workspaces \
  --task "Summarize sales.csv" --backend herdr --accept-edits \
  --ready 120 --wait 300 --dispatch-id herdr-example-1
python3 orchestrator/skill_dispatch.py verify --root workspaces \
  --dispatch-id herdr-example-1 --skill summarize-csv
```

A blocked startup is reported on stderr with the worker's tab and pane. Resolve
the prompt there; dispatch observes readiness within `--ready` and never answers
the prompt itself. After a blocked startup, interactive readiness must remain
stable for three seconds. Herdr needs more than three seconds remaining to start a
worker; its individual startup wait is capped at 300 seconds. New shells that
are still initializing are retried within the readiness budget. Submission uses
Herdr's prompt activity gate with a ten-second timeout and returns as soon as
activity is observed; `--wait` separately controls receipt waiting. The result's
`activity_observed` field records that evidence, which does not prove task completion.
A timeout or failed prompt acknowledgement leaves the tab open for inspection
and reports its identifiers. Inspect that worker before
retrying, since the process or task may already have started. Successful workers
also remain open so their output can be reviewed with `herdr agent read NAME`.
The demo waits for each receipt before dispatching the next task and then verifies
all three receipts.

## Skill visibility and validation

A parent-folder session does not initially load the child workspaces' skills.
Starting inside `calc-lib` makes `add-calc-function` available locally. Claude
can subsequently discover nested skills when it works on files in those folders;
see [Claude's skill discovery documentation](https://code.claude.com/docs/en/skills).
The working directory determines initial discovery, not lifetime isolation.

An untrusted folder can require a human trust decision in an interactive session.
Orca dispatch reports a blocked tab and waits within `--ready`; it sends only
after a positive readiness result and never answers the trust prompt. Local
dispatch uses noninteractive `claude -p` with stdin disabled.

The test suite mocks worker subprocesses and Orca/Herdr responses, including permissions,
folder selection, trust waits, timeouts, send acknowledgements, Git skill identity and receipt checks.
It runs the real dispatcher demo with the print backend and checks Herdr demo
arguments with a stub dispatcher. These tests do not establish
that a live Claude/Orca/Herdr installation starts workers or completes the example tasks.
