# Changelog

All notable changes to this project are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **A character the console could not print no longer ends the run.** The runner
  echoes worker-written text to stdout, and a worker's summary carrying `<=`
  killed a live 02:00 scheduled run on 2026-09-07: Task Scheduler's `cmd.exe`
  runs at cp1252, Python's default `strict` errors raised `UnicodeEncodeError`
  inside `Log.__call__`, and the exception unwound the whole run - *after* the
  worker had finished and committed, but *before* the outcome was recorded. The
  work survived orphaned on its scratch branch while the plan said nothing had
  happened, which is the worst shape a failure can take: the morning cannot see
  it.

  `main()` now sets `errors="replace"` on stdout and stderr, and `Log.__call__`
  carries a fallback for a stream that cannot be reconfigured. The echo is lossy;
  `run.log` is opened UTF-8 independently and still holds every character, so
  nothing is lost but a `?` on a console that could not have shown the character
  anyway. `tools/progress.py` got the same treatment - it prints lines lifted
  straight out of `run.log`.

  This could never have been fixed by writing careful ASCII in this repository:
  the console's encoding belongs to whoever launched the run, and worker text is
  arbitrary. Note it is invisible from an interactive PowerShell, which is UTF-8 -
  the self-test case pins `PYTHONIOENCODING=cp1252` deliberately.

### Added

- **A circuit breaker for the usage wall, and a run that waits it out rather
  than burning the plan against it.** When a worker returns nothing at all -
  exits non-zero having produced no result event - the runner now counts it. Three
  of those in a row (`run.wall_threshold`, `--wall-threshold`) means nothing can
  succeed right now, and the run stops treating the plan as the problem.

  The step it happened on is recorded **`NOT RUN`**, never `STUCK`: no worker read
  the code, so nothing about the code was learned, and its note says so. `NOT RUN`
  is resumable, so a later launch picks it straight back up, and it is not a
  blocking outcome, so `--mode` does not report the plan as BLOCKED for it.

  What happens next is `run.on_wall` / `--on-wall`:

  - **`park` (the default)** waits and probes every 30 minutes
    (`run.park_poll_min`, `--park-poll-min`) until the account answers, then
    resumes at the step it was on. The probe is a haiku worker with no tools and a
    one-word answer, so testing "is the account alive" costs a rounding error
    rather than re-spending a build brief. While parked it logs every five minutes -
    a parked run and a hung run must not look alike.
  - **`stop`** ends the run there, leaving every remaining step pending.

  Parked time does **not** extend the stop time: `--hours` is a promise about when
  you can look, not a quantity of compute owed. `SUMMARY.md` says how much of the
  run went to waiting, over how many probes and at what cost, so the per-hour
  figures cannot quietly lie about what the night bought.

  This is the fix for the run of 2026-09-07, where the account's five-hour window
  closed mid-run and the runner - unable to tell "this step failed" from "nothing
  can succeed" - retried, diagnosed and marked `STUCK` thirty-odd untested steps
  in about twenty minutes. The detection is deliberately **not** a search for a
  quota message in the error text: that would be brittle and would miss a
  logged-out CLI, a withdrawn model or a dead network, which fail the same way and
  deserve the same answer.

- `tools/progress.py <project dir> ...` prints **one line per project** and
  nothing else: whether a run is live, the step it is on this minute, how far
  through the plan it is, and what it has spent. It exists for the question asked
  at 3am, where a report that needs reading is a report that does not get read.
  Read-only - it takes the liveness from the run's own `.lock` (so it needs no
  process table and no arguments beyond the project directory), reads `run.log`
  and the plan, and writes nothing, so it is safe against a run in flight, which
  is the only time anybody wants it. A lock whose pid is dead reports as a
  finished run rather than a live one.

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
