# Herdr validation

Validated on 2026-10-13 with Herdr 0.8.2 (protocol 20), Claude Code 2.1.296,
and Python 3.12.3, from the `herdr-support` worktree.

## Automated checks

- `python -m pytest -q tests`: **198 passed**.
- `bash -n scripts/demo.sh`: passed.
- `git diff --check`: passed.

The tests cover Herdr context requirements, background tab targeting, permission
arguments, shell startup races, startup timeout limits, blocked startup,
interactive readiness, worker identity, prompt activity/error responses, and
Herdr demo dispatch/receipt-wait arguments. They use mocked workers.

## Live demo

Run from a terminal in the Herdr worktree:

```bash
PYTHON=/tmp/skill-dispatch-herdr-venv/bin/python scripts/demo.sh herdr \
  /tmp/skill-dispatch-herdr-live5 --accept-edits \
  --allow-tool 'Bash(python3 -m pytest -q)'
```

The demo exited with status 0. Dispatch ID:
`61feb8fe0ba34067be1679d069497d4e`.

All three dispatches reported `accepted: true`, `activity_observed: true`, and
`receipt: true`. Final verification returned `state: ok` for all three receipts,
including skill identity, token, absolute cwd, and current dispatch ID.

Artifact checks:

- Calculator: `multiply(a, b)` and `test_multiply` were added; an independent
  `python -m pytest -q` run passed both calculator tests.
- Release notes: `0.2.0` appears above `0.1.0`, with “Added a multiply function.”
- CSV report: 4 rows, amount total 445, regions north/south, months
  2026-08/2026-09, matching the source CSV.

This was an interactive validation. Folder-trust and a receipt-writing shell
prompt were resolved with explicit user approval. The dispatcher did not answer
these prompts. Conservative permissions can still require human input.

The scratch artifacts and full run log were retained locally at
`/tmp/skill-dispatch-herdr-live5` and `/tmp/skill-dispatch-herdr-live5.log`.
