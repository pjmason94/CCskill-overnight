# Field report: the first real run

FinKit, run `stage-3b-5b`, 2026-09-06. Two sessions - run 1 at 10:10 stopped by
the operator, run 2 at 10:41 exit 0. Skill at `b38a6eb`, SELFTEST PASS both
times, `isolation: in-place` deliberately, so that a failure would name one thing.

**The first time a real `claude -p` worker has ever run through this runner.**
Everything before this was `fake_worker.py`. The runner did not cause a single
failure across two sessions.

## Proven under real conditions

- The whole worker path: spawn -> work -> gate -> commit -> next step ->
  `SUMMARY.md` -> exit 0. Three real workers, no defect in the loop.
- **The quarantine and the rescue tag both held on their first real test.** A
  gate failed, the tree reset, the untracked file was quarantined with all 123
  lines intact, and the discarded commit was kept as
  `rescue/3b-ref-params-test-attempt-1/1` and listed in `discarded-commits.md`.
  Both were recovered with one `git cherry-pick`, the same day they were needed.
  These were built on 2026-09-05 against a hypothetical and paid for themselves
  within a day.
- Stale lock takeover worked silently after run 1 was killed: "taking over a
  stale lock from pid 89808, no longer running".
- `--mode` / `--only` / `--from` are the right shape. `--mode` said RUN, 23 of 33,
  and named the next step; `--only` resumed precisely.
- Per-step telemetry (exit, minutes, turns, $) is the most useful line in the log.
- A reflect step earned its cost: 6 minutes at opus/high found a dangling grounds
  document, a stale brief under a completed step, and a step ordering whose wrong
  branch would have left holes in a mapping a later step reads.

## Defect 1 - `clean_tree` contradicts the worker preamble. THE MAIN ONE.

The preamble tells a worker: *if `git status` shows a file you did not write,
leave it alone, do not commit it.* The run-level `clean_tree` gate then fails the
step for that same file. The worker cannot win.

Observed: worker exit 0, its own summary reading "Committed and clean, aside from
another window's untracked file" - it obeyed the preamble explicitly and was
failed for it. A good build (735 -> 737 tests) was discarded and attempt 2
relaunched into the same unwinnable state. The operator stopped the run to avoid
about $11 of guaranteed failure. The stray was the operator's own session log,
written into the repo mid-run.

**The runner turned an operator error into a worker penalty, which is the wrong
target.** Worse, it is a REPEATING, RETRYING, BILLABLE loss of good work: every
attempt fails identically until the attempts run out.

### The suggested fix does not work as stated, and the naive fix breaks a guard

The suggestion was to diff against the preflight snapshot rather than against
empty. **That mechanism already exists**: `snapshot_untracked()` runs at the top
of every step, `tree_dirty(ignoring=...)` skips untracked paths that were already
there, and the runner even logs "N untracked file(s) present before this step; the
clean-tree gate will ignore them". It did not help here because the file arrived
DURING the step, after the snapshot. Re-snapshotting later cannot help either: the
moment the worker exits is the same moment the stray is observed.

And the obvious relaxation - "untracked files never fail `clean_tree`" - would
break a guard the self-test relies on. The `fail-dirty` fixture has the worker
write its test file and NOT commit it. The named gate (`pytest -q tests/test_s2.py`)
PASSES, because the file is on disk; only `clean_tree` catches that the work was
never committed. Untracked-means-fail is load-bearing.

The real problem is that the runner cannot tell *the worker left work uncommitted*
from *a third party created a file mid-step*, because both look like a new
untracked path.

### Two fixes that do work

1. **Worktree isolation makes it structurally impossible** - and it is already
   built, just off. The operator's file lands in the operator's tree; the
   worktree's `clean_tree` sees only what the worker did. This is the strongest
   argument yet for making `isolation: worktree` the default, and it moves that
   from a tidiness item to a correctness one.
2. **Attribution from the worker**, for in-place runs: add `files_written: []` to
   the build worker's structured output. `clean_tree` then fails on an untracked
   path the worker CLAIMS, and quarantines-and-warns on one it does not. An
   unclaimed stray is never committed, so a lying or forgetful worker costs
   nothing beyond a warning, while genuinely uncommitted work still fails as it
   must.

Whichever is chosen, **the preamble and the gate must agree**. The pair as it
stands is the defect; either half alone is fine.

## Defect 2 - the gate failure message reads as a diagnosis and is not one

    FAIL attempt-1 - failed the gate: tree clean (the worker committed)

"(the worker committed)" is only the DISPLAY NAME of a `clean_tree` gate
(`overnight.py:300`). It parses naturally as "the worker committed, and that was
the problem", which is the opposite of the truth. Misread on first pass; the
source had to be consulted.

**Fix:** on failure print the offending paths, not the gate's label -
`clean_tree FAILED: 1 untracked path not present at preflight: <path>`. The runner
already enumerates them one line later for the quarantine, so the information is
in hand.

## Defect 3 - a reflect worker cannot write a file it correctly wanted to write

A reflect step diagnosed a missing document and tried to restore it: "Could not
restore the file to its canonical path: the shell refused Copy-Item and nobody is
awake to approve; flagged for Paul." It did the right thing - wrote a pointer
file, repointed eight briefs, escalated - but the permission is narrower than the
job implies.

**Confirmed in the code**: `worker_argv` gives build and review workers
`--permission-mode bypassPermissions`, and reflect and diagnostic workers
`acceptEdits`, which allows edits but not arbitrary shell. So a reflect step is
trusted to rewrite the plan and commit it, but not to copy a file. That asymmetry
is not defensible; decide what a reflect may do and grant exactly that.

## Smaller items

- **Heartbeats cost an orchestrating session a turn each** and re-bill its
  context. They belong in the file, not on stdout: a `--quiet-heartbeat`, or
  simply write them to `run.log` only.
- **No run-level cost total.** Per-step is there; the sum decides whether to
  launch the next stretch. `cost_so_far()` already computes it from the ledger -
  it just is not in the final line or `SUMMARY.md`. One step was $9.92 / 156 turns
  / 26.5 min, so an eight-step run is plausibly $60-80 and the runner should say
  so at the end.
- **The commit trailer names the wrong model.** A step spawned `sonnet/medium`
  produced "Co-Authored-By: Claude Opus 5 (1M context)". The trailer comes from
  the child session's own attribution settings, not from the `--model` the runner
  passed, so commit attribution is unreliable. The runner knows the tier and could
  say so in the brief.
- **Worktree isolation is still unproven** against a real worker. Leaving it off
  for this run was correct. Defect 1 now makes proving it a priority.

## What was done about it, the same day

Every defect above is closed, in three commits on 2026-09-06.

- **Defect 1** - `run.isolation` now defaults to `worktree`; `in-place` is the
  opt-out. Fix 1 of the two offered, chosen over attribution because it is
  structural rather than heuristic. Selftest case 14 is the guard, and it was
  shown to reproduce this incident exactly against the old default: STUCK after
  two identical failures, the good build discarded to a rescue tag, the
  operator's file quarantined out of their own tree. **Still unproven against a
  real worker** - that is the open item.
- **Defect 2** - a failed `clean_tree` prints the offending paths instead of the
  gate's display name.
- **Defect 3** - reflect and diagnostic workers get `bypassPermissions`, like
  build and review. What fences them is not the permission mode: a reflect's
  changes outside the plan and brief directories are reverted, and `safe_reset`
  sits under both.
- **The smaller items** - the heartbeat writes every beat to `run.log` and echoes
  every tenth; the run's cost total is on the last line; the ledger records
  `tier:`, the model and effort the runner asked for, because the commit trailer
  cannot be trusted for it.

Two fixture defects surfaced while building the guard, both of which had been
hiding this class of failure from the self-test: `fake_worker` committed with
`git add -A`, so in-place it swept the operator's file into the worker's commit
rather than failing the gate; and the main fixtures had to pin `isolation:
in-place`, or every in-place defence they exist to test would have gone untested
the moment the default moved.

## Bottom line

The one failure was the operator's, and the safety nets caught it completely.
Defect 1 is the only thing to fix before an unattended overnight stretch, because
it converts any stray file - including one written by the operator's own session -
into a repeating, retrying, billable loss of good work.
