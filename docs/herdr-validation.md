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
