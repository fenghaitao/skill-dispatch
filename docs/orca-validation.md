# Orca validation

Checked on 2026-10-14 with Orca 1.4.224, Claude Code 2.1.300, GitHub Copilot
CLI 1.0.98, Codex CLI 0.160.1 and Python 3.13.7, using the current dispatcher
design (no receipts). Each demo copied the example workspaces into fresh
scratch Git repositories, so every worker started in a folder it had not
trusted yet.

## Problems found and fixed

Before these checks the Orca backend accepted only `--agent claude`. Probing
Copilot and Codex in Orca terminals showed three problems, two of which also
affected Claude:

- **Shell alias widened permissions.** Orca types `--command` into the
  terminal's login shell. With `alias copilot='copilot --autopilot --allow-all'`
  in `~/.bashrc`, the worker ran as
  `copilot --autopilot --allow-all --no-ask-user`, and its status line showed
  "Autopilot · Allow All". Dispatch now starts `command <agent> ...`. With that
  prefix the same shell ran `copilot --no-ask-user ...` with only the requested
  options, and the status line showed "Interactive · Manual Approval".
- **Idle reported before the trust dialog.** A 20-second
  `terminal wait --for tui-idle` returned `satisfied: true` for a new Copilot
  worker. Copilot then drew its folder-trust dialog, and later waits reported
  `blockedReason: agent-trust-workspace`. The dialog's default is "1. Yes", so a
  task sent with Enter at that moment would have answered it. This was seen
  once, in the first probe, while the alias above was active; later probes and
  the demos reported the trust dialog as blocked from the start. Dispatch now
  requires readiness to hold for 3 seconds in a row, restarting whenever a wait
  had to block before reporting idle. The hold is a guard against the observed
  ordering, not a fix verified against a reproduced race.
- **Wait timeout treated as a failure.** While a worker is neither idle nor
  blocked, for example still starting, `orca terminal wait` exits 1 with
  `{"ok": false, "error": {"code": "timeout"}}` instead of an unsatisfied
  result. A Claude probe showing its trust dialog also got only these timeouts,
  never a `blockedReason`. Dispatch used to abort on the first one, after at most
  one 30-second wait. It now keeps waiting within `--ready`. As a consequence, a
  worker that exits before it becomes ready makes dispatch wait out the
  `--ready` budget instead of failing at once.

## Demo runs

All three demos dispatched every task with `accepted: true`. While each worker
waited on its folder-trust dialog, dispatch printed
`blocked on agent-trust-workspace; answer it there`. The trust dialogs were
answered by hand in the scratch folders: "1. Yes" (this session only) for
Copilot, "Trust and continue" for Codex, and "Yes, I trust this folder" for
Claude.

- `AGENT=copilot scripts/demo.sh orca <scratch> --accept-edits --allow-tool
  'shell(python3)'`
- `AGENT=codex scripts/demo.sh orca <scratch> --accept-edits`
- `scripts/demo.sh orca <scratch> --accept-edits --allow-tool
  'Bash(python3 -m pytest -q)'`

Each run produced the same results: `multiply(a, b)` and `test_multiply` were
added and pytest passed (2 tests); the 0.2.0 release note sits above 0.1.0; and
`reports/sales.md` matches the source (4 rows, `amount` total 445, regions
north/south, months 2026-08/2026-09).

## Observations

- Orca's prompt receipt reported only `input_accepted` for every worker in the
  demos. For Copilot it reports `provider: unsupported` with the warning that
  delivery cannot be observed. A direct Codex probe with
  `orca terminal send --wait-submit 15` did observe `turn_started`. Dispatch
  does not wait for submission.
- Orca reported a Copilot worker idle while Copilot showed a command approval
  prompt. An idle Copilot terminal therefore does not mean the task finished.
- Claude Code 2.1.300's trust dialog selects "No, exit" by default. A first
  Claude demo was stopped after an Enter typed into that dialog by hand exited
  the worker. With nothing to wait for, Orca's waits timed out until the
  terminal was closed. The demo was then rerun as described above.
- With `permissions.defaultMode: "auto"` in `~/.claude/settings.json`, one
  Claude worker started with `--permission-mode acceptEdits` was recorded as
  switching to auto mode at its first command that `acceptEdits` did not allow.
  The dispatcher sent nothing to that worker after the task. The dispatcher
  controls only the command line; settings files still apply.
