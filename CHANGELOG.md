# Changelog

All notable changes to this project are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.1] - 2026-09-06

### Changed

- **No worker of any kind can now reach a tool that acts outside the tree.**
  `UNATTENDED_DENY` denies `Artifact`, `CronCreate`, `CronDelete`, `CronList`,
  `DesignSync`, `PushNotification`, `RemoteTrigger`, `SendMessage` and `Workflow`
  to build, review, reflect and diagnostic workers alike. Each of them publishes
  a page, schedules or fires work that outlives the run, messages somebody, or
  fans out further agents - none of which the git undo underneath this runner can
  reach, with nobody awake to see it happen. The review step's
  `Edit`/`Write`/`NotebookEdit` fence is unchanged and now sits on top of that
  list, in a single `--disallowedTools` rather than two. A tool name the installed
  CLI does not have is inert, so the list can safely carry one only some versions
  ship. Roughly 4% of a run's tokens go with them, which is a bonus and not the
  reason.
- Every worker's brief now also tells it to **keep tool results small** - a
  result is paid for when it arrives and again on every call after it, which is
  the largest component of the 56% of a run's spend that is context re-reads. In
  practice: Grep to locate before Read, `offset`/`limit` on a large file, no
  re-reading what is already in context, filter a noisy command rather than
  dumping it. Nothing is forbidden; it changes what a worker reaches for first.

### Added

- `SUMMARY.md` now **splits the run's cost into building and not-building** -
  review and reflect steps, and the rework they order - with counts of the build
  steps that took more than one attempt or were reworked after a review. The
  efficiency bar is structural (review, reflect, rework and discarded attempts
  were 26% of the first real run), so it is now a number the operator sees every
  morning rather than one a document asserts. The kind split is exact; the
  retried/reworked counts are indicative, because the ledger keeps one cost per
  step and a step's discarded attempts sit inside its own figure.
- Every worker - build, review, reflect, diagnostic - is now told, in its brief,
  to prefer `Read`, `Grep`, `Glob` and an Explore-style subagent (if the Agent
  tool is available to it) over Bash for reading and searching the codebase, and
  to reserve Bash for the test suite, build scripts and git. `cat`/`sed` reads
  cost far more input tokens than the equivalent structured call; this does not
  restrict what a worker *can* do (Bash stays fully available for gates and
  commits), only nudges what it reaches for first.

### Fixed

- **A rework's cost never reached the ledger.** `run_build` charges its workers
  to the step it is building, but a rework rebuilds a step that was recorded long
  before and whose cost was flushed with it, so the spend sat in memory until the
  process exited and was missing from `SUMMARY.md` and from every `--mode` cost
  total. It is now attributed to the review step that ordered it, which is also
  where it belongs when reading the overhead split. The self-test's new guard is
  the invariant itself: the ledger total must equal what the worker logs say was
  spent, whatever path spawned them.
- README section 12's permission table still said reflect and diagnostic workers
  ran under `acceptEdits`. That was changed to `bypassPermissions` before v1.0.0
  was even cut (a reflect must be able to do more than edit and commit; a
  diagnostic must be able to run the failing test it is asked to explain) - the
  doc just never caught up.

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
