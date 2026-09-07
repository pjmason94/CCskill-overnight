# Roadmap

What is not built yet. Each item names why it matters and what it costs.

**`FIELD-REPORT-2026-09-06.md` is the first real-conditions evidence this project
has, and it outranks everything below.** Its defect 1 - `clean_tree` failing a
step for a file the preamble told the worker to leave alone - is the only thing
that must be fixed before an unattended stretch, and worktree isolation turns out
to be one of its two real fixes.

What has been built is listed at the bottom. Items 1 and 2 have designs under
`design/`, written before any of their code; item 2's cheap interim is built and
item 4's honest answer - "unverified off Windows" - is now stated in the README
rather than left implied.

## 12. A stall watchdog: a silent worker must not be paid for to the timeout

**Measured, 2026-09-07.** Twice in one night a worker went silent - the process
alive, the log file frozen at a fixed size - and each burned the full 90-minute
`worker_timeout_min` before `exit 124` killed it. FinKit `5b-liveness` attempt-1
sat frozen at 958 KB for 60 minutes and then PASSED on attempt-2 in 27 minutes;
`5b-recognise` attempt-1 froze at 351 KB and cost its step 90 minutes, after
which attempt-2 finished in 47. Roughly two and a half hours of a run's wall clock
went to waiting on two processes that had already stopped working.

`timeout_min` cannot be the answer. It has to be set for the slowest step the run
legitimately contains - and `5b-recognise` took 47 real minutes, so 90 is not
generous - which means it can never catch a stall early. It is a backstop against
a runaway, not a detector of a stopped one.

The signal already exists and nothing acts on it: the heartbeat measures the log
file's size every minute and prints it. A worker that is working writes
continuously (that step wrote 2.6 MB across 119 turns, and no minute of it was
flat for long). So: if the log has not grown for `stall_min` (a default in the
region of 10-15 minutes, and it must be a spec key because a step that runs one
very long gate is legitimately quiet), kill the worker and treat it as a failed
attempt - the retry is the fix, and on both of these it was.

Two things to get right. A quiet gate is not a stall: the runner knows when it is
running a gate rather than a worker, and the clock must not run then. And the
outcome must be distinguishable in the ledger - a `STALLED` attempt note, so the
morning can tell "the worker stopped answering" from "the worker tried and the
gate failed", which are different problems with different fixes.

Cheap: one comparison in the existing heartbeat loop, the kill path is the one
`timeout` already uses, and the self-test fixture is a fake worker that prints
nothing and sleeps.

## 11. `--until`, not `--hours`: the operator's constraint is a deadline

**The metric is wrong.** `--hours` asks for a duration. What an operator actually
has is a moment - "I want to see what has been achieved by 07:30". Every launch
therefore begins with the operator converting their real constraint into the
runner's, in their head, at the exact moment they are least equipped to do
arithmetic: last thing at night. On 2026-09-06 that conversion was done four times
in one evening between two projects and got it wrong once, in the direction that
would have cost an entire eight-hour run.

**A duration is also unstable under the launch it is designed for.** `--hours 8`
in a scheduled task means eight hours from whenever the task fires. If the trigger
is missed, or the first launch refuses on a dirty tree and the operator relaunches
twenty minutes later, the run overshoots the morning by exactly the delay - and it
does so silently, because nothing in the run knows what time the operator meant.
A deadline is invariant to when the launch actually happened, which is precisely
the property an unattended, scheduled thing needs.

**It makes the parking rule obvious rather than arguable.** Parking already
does NOT extend the stop time, which under `--hours` is a defensible choice
rather than an evident one: a three-hour wall silently eats most of a run whose
operator asked for six hours of work. Under `--until` there is nothing to argue
about - the promise is a moment, so a park eats into the work and the deadline
does not move, which is the operator's actual intent. They asked to see results
by a time, not to be given a fixed quantity of compute whenever it could be
spent.

**It lets the runner stop honestly, which `--hours` cannot.** Today the clock
stops the runner STARTING steps; a step already running carries on, so the real
end is the deadline plus however long the last step takes - up to half an hour
past, on the step lengths seen so far. An operator reading "results by 07:30"
means results, not a step still running at 07:52. With a deadline and the
`expected_min` each step already carries (item 3), the runner can decline to start
a step it cannot finish in time, and say so: `not starting b5-2 (est 18 min,
14 min left)`. That is a better answer than starting it and being reset by the
clock, and it is the first thing that would make `expected_min` earn its place.

**Shape.** `--until 07:30`, resolved to the next occurrence of that time and
logged ONCE as an absolute datetime at the top of the run, so the log is never
ambiguous about which 07:30 was meant. `run.until` in the spec beside `run.hours`.
Keep `--hours` working as a synonym computed against the start - it is a running
contract, deprecated rather than removed, and the change is noted in `README.md`
and the changelog when it lands.

## 1. The expected-value triage on a raised judgement

**The idea.** Today a step that needs judgement stops and leaves the decision for
the operator. Instead, an LLM pass classifies it first: is this a call the runner
should take now, or one that must wait for a human?

**The rule is expected value, not category.** Proceed if

    P(wrong) x rework_cost   <   time saved by not stopping

**Three refinements, all load-bearing:**

1. **Rework cost is measured over the DEPENDENCY FANOUT, not the step.** A wrong
   call in the last step of a run costs one step; the same call at step 3 of 28,
   with eight steps building on it, costs those eight plus reintegration. The same
   judgement at the same confidence is therefore right to take late and wrong to
   take early. The runner knows the pending plan, so the fanout is computable.
2. **Stacking needs a budget.** Ten decisions at 90% confidence each leaves 0.35
   that the whole chain is sound. Keep a **running confidence product** for the
   run; when it falls below about 0.5, stop taking new judgement calls and bank
   what is built.
3. **The discriminator is DETECTABILITY, not importance.** A wrong naming call is
   obvious in a diff. A wrong semantic call can produce output that passes every
   gate and reads correctly, and a review cannot catch what looks right. So the
   override is "would the morning report make this error visible?" - and that is
   cheaper to fix than to avoid: every resolved decision leads the report with its
   confidence, the alternative rejected, and a one-line reason.

**Two consequences.** Prefer the REVERSIBLE implementation where there is a choice
- an isolated commit is a `git revert`, the same change woven through five files is
a re-run - which lowers the rework term rather than merely estimating it. And note
that stopping is cheaper than it first appears: a step that stops does not abort
the run, it skips, and everything not depending on it proceeds. Both sides of the
comparison are the same quantity, the dependent subtree.

**Never:** the triage classifies and injects a step; it never implements. The
injected step faces the normal gates and review like any other.

**The design is `design/decision-triage.md`.** Note what it found: there is no
structured way for a worker to raise a judgement today. `DECISIONS-PENDING.md` is
free text written by whatever the project's own preamble tells the worker to
write. The hook has to be built before the triage has anything to triage.

## 2. Each worker in its own git worktree - BUILT and DEFAULT 2026-09-06

Built to the design in `design/worktree-isolation.md`; what follows is why it
was wanted. It was opt-in for a few hours, on the reasoning that a change making
every gate run in a different directory should not become the default the week it
is written. The first real run overturned that the same day: in-place, a
`clean_tree` gate cannot tell the worker's uncommitted work from a file the
operator wrote mid-step, so it failed a good build for the operator's own session
log and would have kept failing it (see `FIELD-REPORT-2026-09-06.md`, defect 1).
That made isolation a correctness item rather than a tidiness one, and
`run.isolation: in-place` the opt-out. **It is still unproven against a real
worker** - the remaining work on this item is a real night on it.

Two things the build learned that the design did not:

* the worktree must be removed BEFORE integrating, not after. A replay has to
  check the scratch branch out, and git refuses to check out a branch another
  worktree is holding. The commits are on the branch, so the directory has
  already done its job.
* the step's note must be REPLACED at integration, not appended to. The attempt
  loop writes "committed at <sha>" for the commit as it stood in the worktree,
  and a replay rewrites that sha - so the note named a commit that was not on the
  branch and could not be looked up in the morning.


**The problem this closes.** The runner shares one branch and one working tree
with whoever else is at the keyboard. The current defences are good but they are
defences: worker commits are stamped with a run-specific committer identity, a
reset tags and logs everything it would discard, a reset that would remove a
foreign commit is refused and the step HALTED, and `git clean` quarantines rather
than deletes. All of that turns silent destruction into a loud refusal - which is
the right trade, but a halted step is still a step lost, and the clean-tree gate
can still be failed by a file somebody else created mid-step.

**The fix.** Run each worker in its own `git worktree` on a scratch branch, gate
it there, and fast-forward or cherry-pick onto the operator's branch only once the
gates pass. The operator's tree and branch are then never touched by a reset, the
whole class disappears, and "do not touch the repo while a run is live" stops
being load-bearing advice.

**What it costs.** Real work: worktree lifecycle (create, prune, orphan cleanup
after a crash), gates that assume the project root, absolute paths in briefs, and
disk for a checkout per concurrent worker. Also a decision about what a review
step reads - the scratch branch, or the merged result.

**The interim is now built** (2026-09-06): `overnight/runs/.lock` holds the
repository, not the run; a second runner refuses to start and says which run
holds it; a lock whose process has died is taken over; a report is never blocked
by it. That stops two runners fighting. It does not stop the operator's own
window from committing mid-step, which is what the worktree closes.

**The design for the worktree itself is `design/worktree-isolation.md`.**

## 3. `expected_min` used, not just recorded

`expected_min` is now recorded beside the actual and shown in `SUMMARY.md` as a
ratio, with anything over 1.5x marked. The next move is to use the history: read
previous runs' actuals when planning, and warn at plan time when a step's estimate
is out of line with what steps of that shape have actually taken. Needs runs to
accumulate first.

## 8. Hold the run to the efficiency bar

`docs/token-efficiency.md` measured where an overnight run's tokens go, and found
that headless workers cost the same per API call as an interactive session - so
the 2x risk is structural, not per-call. Three things follow from it, none built:

1. ~~**Bound what a tool result leaves behind.**~~ DONE. `TOOL_USAGE_NOTE` now
   tells every worker kind that a tool result is paid for on arrival and again on
   every call after it: locate with Grep before reading, Read a large file with
   `offset`/`limit`, do not re-read what is already in context, filter a noisy
   command. Free, and aimed at the largest single contributor to the 56% of spend
   that is context re-reads. Whether it MOVES that 56% is unmeasured - the next
   real run on 2.1.263 is the first test of it.
2. ~~**Trim the worker's tool list.**~~ DONE. `UNATTENDED_DENY` in `overnight.py`
   denies `Artifact`, `CronCreate`, `CronDelete`, `CronList`, `DesignSync`,
   `PushNotification`, `RemoteTrigger`, `SendMessage` and `Workflow` to every
   worker kind, merged into the single `--disallowedTools` the review step was
   already using. Safety first - none of them is reachable by the git undo; the
   ~4% token saving is a bonus.
3. ~~**Report the structural overhead per run.**~~ DONE. `overhead_line()` splits
   `SUMMARY.md`'s total into building and not-building, with counts of the build
   steps that were retried or reworked. Doing it surfaced a defect: a rework's
   cost never reached the ledger at all, so every run to date has under-reported
   itself by whatever its reworks cost. Fixed, and guarded by the invariant
   (ledger total == what the logs say the workers spent).

The subagent question is deliberately NOT on this list as a change: a subagent
pays its own prefix and does the same reading, so it only saves what the reading
would have left behind, and a parallel fan-out costs more than it saves. It needs
a measured experiment on one real step first.

## 7. A plan seed: `/overnight plan <file>`

**The idea.** The planner reads documents the user names - a roadmap, an issue
list, a design note - proposes the step list from them, and interviews only on
what it cannot infer from them. Today stage 1 starts from the project and the
conversation, so a user who has already written down what they want says it
twice.

**A path, not a format.** The argument names existing files; the skill does not
define a "build plan" shape for the user to write to. Requiring a shape -
heading per step, bullet per gate - would mean that by the time somebody has
written it they have written `steps.yaml`, and the project would then carry two
documents to keep in sync, which a reflect step rewriting the plan mid-run makes
worse. Any prose the user already has is a legitimate seed.

**What it does not save.** The gates. A roadmap says what should be true, almost
never how you would know it is - and turning that into a command that exits 0 is
the slow half of planning and the half that makes a run unattendable. The seed
saves the enumeration, not the judgement.

**The one rule that keeps it honest.** The planner may merge, split, reorder or
drop what it reads - but it must then LIST what it dropped and why, at the top of
the proposal. A seed that is silently filtered is worse than no seed: the user
believes their document is the plan.

**What it costs.** Small, and stage 1 only: an argument through `SKILL.md`, a
section in `references/planning.md`, no runner change and so nothing that can
affect a live run.

## 4. Cross-platform verification

Developed and run on Windows. `fresh_shell` branches - PowerShell with the PATH
rebuilt from the registry on Windows, a login shell on POSIX - process
termination branches on `os.name`, and the lock's liveness check branches too
(`tasklist` on Windows, because `os.kill(pid, 0)` on Windows does not test a
process, it terminates it). **None of the POSIX branches has ever been
executed.** The README now says so plainly - "Windows-only, until somebody runs
the self-test elsewhere" - which was the honest half of this item and is done.
What remains is the actual verification: `python selftest.py` on Linux and on
macOS. It needs `git`, `pytest` and a shell, and it will say.

## 5. Packaging

Not on PyPI, no `pyproject.toml`, no version number, no tag. `install.py` covers
installing from a checkout, which is the only route today. Packaging matters when
somebody who is not cloning the repository wants the skill; it does not before.

## 6. A worked example with a real morning

`examples/` runs end to end against the fake worker and shows the mechanism. It
does not show the part that is hardest to teach: reading the morning's output and
deciding what to do about it. A recorded, anonymised `SUMMARY.md` with a real
mixture of passes, a STUCK step, a review that found something real, and the
decisions that followed, would be worth more than any amount of prose about it.

---

## Done

- **A circuit breaker, and a run that waits out a usage wall** (originally items
  9 and 10). The runner counts BARREN worker invocations - exited non-zero AND
  produced no result event - and calls the wall at three in a row. The step it
  happened on is recorded `NOT RUN`, never `STUCK`: no worker read the code, so
  the morning is not handed findings about work nobody looked at. Then
  `run.on_wall` decides: `park` (the default) probes every 30 minutes with a
  haiku worker, no tools, one word of output, and resumes at the step it was on
  when the account answers; `stop` ends the run with the rest left pending.
  Parked time does not extend the stop time, and `SUMMARY.md` says how much of
  the night went to waiting. The detection is a threshold, never a search for a
  quota message: a logged-out CLI, a withdrawn model and a dead network fail the
  same way and deserve the same answer. Proven against the unmodified runner,
  which marched through the whole plan marking every untested step `STUCK`.
- **Rescue tags over discarded work** (originally item 3). Every commit a reset would
  discard is tagged `rescue/<label>/<n>`, logged with its subject, and listed in
  `discarded-commits.md` in the step directory.
- **Never destroy a third party's commit** (raised as a risk note, 2026-09-05).
  Worker commits carry a run-specific committer identity; a reset that would
  remove a foreign commit is refused and the step HALTED; `git clean -fd` moves
  untracked files to `quarantine/` instead of deleting them; the baseline is
  re-read per attempt rather than per step. Proven against the previous runner,
  which destroyed the stranger's commit and left it only in the reflog.
- **Degrade without git** rather than refusing to start: warn once, skip
  `clean_tree` gates and review steps, say plainly when a reset cannot happen.
- **Neutral examples and a runnable one** (originally item 5). `examples/` is a complete
  fictional package; `try_it.py` runs it end to end for no tokens.
- **A licence** (originally item 6). GPL-3.0.
- **One-step install.** `install.py` links a checkout in as the skill.
- **The two stages.** PLAN writes a spec into the project; RUN launches it later
  off disk; PROGRESS reads a run; `--progress` does the mechanical part.
- **The steps file is the ledger.** `state.json` is gone. Every outcome is
  spliced into its own step's block as `done:` and committed as
  `overnight: <id> <outcome>`, so the plan file is the single record of the run -
  what was planned, what ran, when, and what it produced, in one hand-readable
  version-controlled file. The splice is textual and verified: it re-parses the
  result and refuses any edit that would change another step, so the operator's
  comments and key order survive every write. Resume, `--list`, `--progress`,
  `SUMMARY.md`, the reflect validation and the exit code all read it; the reflect
  fingerprint map is gone, replaced by comparing completed steps against the
  backup the reflect already takes.
- **One `/overnight`, with the mode inferred.** `--mode` prints `BLOCKED`, `PLAN`,
  `RUN` or `REPLACE?` from the plan file alone, so the skill branches on a fact
  rather than reasoning about file states. BLOCKED is tested first, exits 3, and
  is a hard stop: a STUCK or HALTED step needs a person, and the report names it,
  says why, and gives the user their options as commands they run themselves.
