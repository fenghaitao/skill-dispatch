# Herdr validation

The Herdr backend was validated on 2026-10-13 with Herdr 0.8.2 (protocol 20),
Claude Code 2.1.296, and Python 3.12.3.

This note records backend validation, not the current orchestration protocol.
The original live run predated the simplified design in [design.md](design.md),
which removed dispatcher receipts, synthetic dispatch IDs, and post-run
verification.

## What the validation established

The live and mocked tests exercised:

- inherited Herdr workspace targeting;
- background tab creation without stealing focus;
- Claude agent startup in the selected workspace cwd;
- permission argument forwarding;
- shell-startup races;
- readiness and timeout handling;
- trust/blocked startup behavior without automatic UI answers;
- worker identity checks;
- prompt acknowledgement and observed activity;
- preservation of the worker tab/session for later inspection.

The live demo completed all three example tasks successfully:

- Calculator: added `multiply(a, b)` and `test_multiply`; an independent
  pytest run passed.
- Release notes: added version 0.2.0 above 0.1.0.
- CSV report: produced the expected row count, totals, regions and months.

## Current lifecycle model

The current dispatcher does not ask the worker to write a receipt. Herdr's
workspace/tab/pane/agent identifiers and session log are the execution record.

The dispatcher still captures the selected workspace's Git commit and the
selected skill's blob ID before launch. See [design.md](design.md).

A blocked startup remains a human decision: resolve it in the Herdr tab. The
dispatcher observes readiness but does not answer trust prompts.

## GitHub Copilot workers

Checked on 2026-10-13 with Herdr 0.9.3 client / 0.9.1 server (protocol 22),
GitHub Copilot CLI 1.0.91 and Python 3.13. Like the run above, these live
checks predate the simplified design: completion was then evidenced by
worker-written receipts, which the current dispatcher no longer uses.

- Local demo: `AGENT=copilot scripts/demo.sh local <scratch> --accept-edits
  --allow-tool 'shell(python3)'` completed all three tasks; `multiply` and its
  test were added and pass, the 0.2.0 release note is above 0.1.0, and the CSV
  report matches the source (4 rows, total 445).
- Herdr probes: `herdr agent start --kind copilot` reports `agent: "copilot"`,
  returns `agent_not_ready` while Copilot shows its folder-trust prompt, and
  then reports `idle` with `interactive_ready: true`. `herdr agent prompt`
  acknowledged a task as `working` and the worker reached `done`.
- Launch check: with a `~/.bashrc` alias of `copilot` to
  `copilot --allow-all --autopilot`, a Herdr shell applies it. A live
  `--backend herdr --agent copilot` dispatch found those extra options with
  `herdr pane process-info` and refused to send the task, as intended.
- Herdr demo, with the alias removed: `AGENT=copilot scripts/demo.sh herdr
  <scratch> --accept-edits --allow-tool 'shell(python3)'` completed all three
  tasks with `accepted: true`; the artifacts matched the local demo.
- An earlier Herdr attempt hit `agent_prompt_stalled`: Copilot started the turn
  more than five seconds after Herdr accepted the input, although the task ran.
  Dispatch now keeps observing after that error, never resubmitting.
- Claude + Herdr with the launch check: a live `--backend herdr --agent claude
  --accept-edits` dispatch passed the check and reported `accepted: true` and
  `activity_observed: true`.

Folder-trust prompts, and shell commands outside the `--accept-edits` rules,
were approved by hand in scratch folders; the dispatcher did not answer them.

## OpenAI Codex workers

Checked on 2026-10-13 with Herdr 0.9.3 client / 0.9.1 server (protocol 22),
Codex CLI 0.160.0 and Python 3.13, after the simplified design.

- Local demo: `AGENT=codex scripts/demo.sh local <scratch> --accept-edits`
  completed all three tasks. Codex ran the calculator tests inside the
  `workspace-write` sandbox (2 passed), added the 0.2.0 release note, and
  produced a CSV report matching the source (4 rows, total 445).
- Sandbox: under `workspace-write`, a write to the home directory failed with
  "Read-only file system".
- Herdr probes: without `--no-daemon`, Codex showed a shared-server settings
  modal while Herdr reported `idle` with `interactive_ready: true`. With
  `--no-daemon`, the update dialog and folder-trust prompt were both reported
  as `blocked`; after trust Codex briefly went `working`, then `done`.
- Herdr demo: `AGENT=codex scripts/demo.sh herdr <scratch> --accept-edits`
  dispatched all three tasks with `accepted: true` and `activity_observed:
  true`. No update dialog appeared, and the artifacts matched the local demo.
- Trusted folder: a second dispatch into an already-trusted workspace started
  without prompts, was accepted, and completed the task.

Folder-trust prompts were approved by hand in scratch folders; the dispatcher
did not answer them.
