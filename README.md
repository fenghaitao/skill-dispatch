# skill-dispatch

A small prototype of an orchestrator that discovers the skills of several
folders and dispatches work to an agent in the right folder. The worker finds
the skill in its own folder. Discovery, routing and dispatch read only frontmatter;
verification intentionally reads skill bodies to check receipt tokens.

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
   never read here. YAML frontmatter is limited to 16 KiB, parsed safely, and
   duplicate keys are rejected. Names must be lowercase letters or digits in
   words separated by hyphens, at most 64 characters. Receipt paths must stay
   inside the workspace's `.skill-receipts/` directory, including through symlinks.
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

scripts/demo.sh print /tmp/sd-plan        # destination must not exist
scripts/demo.sh local /tmp/sd-local --accept-edits --allow-tool 'Bash(python3 -m pytest -q)'
scripts/demo.sh orca /tmp/sd-orca --accept-edits --allow-tool 'Bash(python3 -m pytest -q)'
python3 -m pytest -q tests
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

Local dispatch always checks for a complete receipt for the current dispatch
after the synchronous worker returns. A zero worker exit with a missing or
invalid receipt makes dispatch exit nonzero. `--wait` allows extra waiting for
a successful worker's receipt; a failed worker does not trigger extra waiting.

`--wait SECONDS` waits for complete receipt JSON with the selected skill, absolute
folder and current dispatch ID. Waiting does not read skill bodies or check the
secret token; run `verify` afterward to check the token. A send acknowledgement
proves input acceptance, not a started turn; Orca delivery metadata and warnings
are retained in the output. Readiness and receipt wait durations must be finite
and nonnegative. `--ready` applies only to Orca startup and never answers a prompt.

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

The test suite mocks worker subprocesses and Orca responses, including permissions,
folder selection, trust waits, timeouts, send acknowledgements and receipt checks.
It also runs the demo with the print backend only. These tests do not establish
that a live Claude/Orca installation starts workers or completes the example tasks.
