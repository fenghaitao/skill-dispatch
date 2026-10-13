# skill-dispatch

A small prototype of an orchestrator that discovers the skills of several
folders and dispatches work to an agent in the right folder. The worker finds
the skill in its own folder; the orchestrator never loads it.

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
   never read here.
2. **Route.** Pick the skill for a task: the one named with `--skill`, or the
   best word overlap between the task and the skills' names and descriptions.
   No match, or a tie, asks for `--skill`.
3. **Dispatch.** Start a worker with the chosen folder as its working
   directory and tell it which skill to use:
   - `print`: show the plan only;
   - `local`: `claude -p` as a subprocess in the folder;
   - `orca`: register the folder with Orca, open a terminal there, start
     `claude`, wait until it is ready, then send the task.
4. **Verify.** Each demo skill tells the worker to atomically write
   `.skill-receipts/<skill>.json` with its skill name, a token from the skill
   body, the absolute working directory, and the dispatch ID supplied in the
   prompt. The separate `verify` command intentionally reads skill bodies to
   check tokens. It requires `--dispatch-id ID` and fails for missing, invalid,
   stale or mismatched receipts. It checks every discovered skill by default;
   repeat `--skill NAME` to require only the skills dispatched in that run.
   Receipts provide evidence that instructions were followed; they do not
   prove that the requested work is correct or complete.

## Usage

Install the runtime dependency with `python3 -m pip install -r requirements.txt`.

```bash
python3 orchestrator/skill_dispatch.py discover --root workspaces
python3 orchestrator/skill_dispatch.py route --root workspaces --task "Summarize sales.csv"
python3 orchestrator/skill_dispatch.py dispatch --root <dir> --task "..." --backend local --dispatch-id example-run
python3 orchestrator/skill_dispatch.py verify --root <dir> --dispatch-id example-run --skill <selected-skill>

scripts/demo.sh local /tmp/sd-demo        # MODEL=claude-haiku-5-5 for cheap workers
scripts/demo.sh orca  /tmp/sd-demo
python3 -m pytest -q tests
```

`demo.sh` copies each workspace into a scratch folder as its own git
repository (a sub-repo), so runs never change this repository.

## Permissions

Both worker backends explicitly start Claude with `--permission-mode dontAsk`.
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

## Receipt IDs and waiting

Dispatch generates a fresh ID if `--dispatch-id` is omitted and includes it in
the JSON plan and worker prompt. Use a unique ID for each run; never reuse an ID
for a different task in the same workspace. Record the generated ID, or supply
your own unique ID when coordinating dispatch and verification.
Legacy receipts without a dispatch ID fail validation.

`--wait SECONDS` waits for complete receipt JSON with the selected skill, absolute
folder and current dispatch ID. Waiting does not read skill bodies or check the
secret token; run `verify` afterward to check the token. A send acknowledgement
proves input acceptance, not a started turn; Orca delivery metadata and warnings
are retained in the output. Readiness and receipt wait durations must be finite
and nonnegative. `--ready` applies only to Orca startup and never answers a prompt.

## Findings

- A Claude Code session started in the folder that holds the sub-repos sees
  none of their skills; a session started inside `calc-lib` sees
  `add-calc-function`. Skills follow the working directory.
- With both the `local` and the `orca` backend, all three workers wrote
  receipts with the right token and folder, and did the work in their own
  sub-repo (calc.py and its test, the 0.2.0 changelog entry, the sales report).
- A folder Claude Code has not been told to trust opens with a trust prompt.
  The `orca` backend leaves that to a person: it names the waiting tab and
  sends the task once the agent is ready. `claude -p` does not prompt.
