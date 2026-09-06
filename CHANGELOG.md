# Changelog

All notable changes to this project are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.1] - 2026-09-06

### Fixed

- README section 12's permission table still said reflect and diagnostic workers
  ran under `acceptEdits`. That was changed to `bypassPermissions` before v1.0.0
  was even cut (a reflect must be able to do more than edit and commit; a
  diagnostic must be able to run the failing test it is asked to explain) - the
  doc just never caught up.

### Added

- Every worker - build, review, reflect, diagnostic - is now told, in its brief,
  to prefer `Read`, `Grep`, `Glob` and an Explore-style subagent (if the Agent
  tool is available to it) over Bash for reading and searching the codebase, and
  to reserve Bash for the test suite, build scripts and git. `cat`/`sed` reads
  cost far more input tokens than the equivalent structured call; this does not
  restrict what a worker *can* do (Bash stays fully available for gates and
  commits), only nudges what it reaches for first.

## [1.0.0] - 2026-09-06

Initial public release. See `README.md` for the full design; the highlights:

- Four step kinds - build, review, reflect, gate - driven by a steps
  specification, with git as the undo.
- Each build step runs in its own git worktree by default (`run.isolation:
  worktree`), integrated onto the operator's branch only once its gates pass, so
  a file created in the operator's own tree mid-step cannot fail a worker's
  gate. `in-place` is the opt-out.
- A reset quarantines untracked files instead of deleting them, and tags every
  commit it discards as `rescue/<step>/<n>` rather than losing it.
- A reset refuses outright if any commit in range was made by something other
  than this run's own workers.
- The plan file is the ledger: outcomes are spliced into each step's own block
  and committed, so `--mode` alone is a sufficient handoff after a `/clear`.
- `selftest.py` exercises every path against a scripted fake worker in about two
  minutes, for no tokens; passing it is a hard rule before any launch.
- Proven across two real overnight runs the day of release (`docs/`), including
  the defect that made worktree isolation the default and the review-to-rework
  cycle catching two real bugs a build step's own gates had missed.
