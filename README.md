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
4. **Verify.** Each demo skill tells the worker to write
   `.skill-receipts/<skill>.json` with a token that appears only in the skill
   body, and the working directory. A matching token and folder show that the
   worker read the skill from its own folder.

## Usage

```bash
python3 orchestrator/skill_dispatch.py discover --root workspaces
python3 orchestrator/skill_dispatch.py route --root workspaces --task "Summarize sales.csv"
python3 orchestrator/skill_dispatch.py dispatch --root <dir> --task "..." --backend local
python3 orchestrator/skill_dispatch.py verify --root <dir>

scripts/demo.sh local /tmp/sd-demo        # MODEL=claude-haiku-5-5 for cheap workers
scripts/demo.sh orca  /tmp/sd-demo
python3 -m pytest -q tests
```

`demo.sh` copies each workspace into a scratch folder as its own git
repository (a sub-repo), so runs never change this repository.

## Findings

- A Claude Code session started in the folder that holds the sub-repos sees
  none of their skills; a session started inside `calc-lib` sees
  `add-calc-function`. Skills follow the working directory.
- With the `local` backend, all three workers wrote receipts with the right
  token and folder.
- A folder Claude Code has not been told to trust opens with a trust prompt.
  The `orca` backend leaves that to a person: it names the waiting tab and
  sends the task once the agent is ready. `claude -p` does not prompt.
