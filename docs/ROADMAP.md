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

## 9. A circuit breaker: stop when the failures stop being about the code

**The gap.** The runner has no way to tell "this step failed" from "nothing can
succeed right now". A worker that returns nothing because the account's usage
window is exhausted looks exactly like a worker that wrote bad code: the gate
fails, the tree resets, the attempt is retried, a diagnostic runs, the step is
marked `STUCK`, and the loop moves to the next step and does it again. With
thirty-odd steps pending and every attempt failing in seconds, a run can burn
through the entire remaining plan in about twenty minutes and mark all of it
`STUCK`. The operator wakes to a run that reports itself finished, a plan whose
every step needs re-running, and no work done.

`HALTED` exists but covers exactly one case - a third party committing to the
branch mid-step (`overnight.py`, in the attempt loop). Nothing covers the general
one.

**Why it is not solved by scheduling.** This was worked around by hand on
2026-09-06 by timing an overnight launch to start in a fresh usage window, with a
short throwaway run burning the tail of the old one. That protects the first hour
and nothing after it: a seven- or eight-hour run crosses a window boundary in the
middle of the night whatever time it starts. The arithmetic of when to launch is
a symptom, not a fix.

**The shape.** Count consecutive steps that end in a failure outcome having
produced NO commit. At a threshold - two is probably right, three at most - stop
the run rather than continue: the run ends the way the clock ending it does, with
`SUMMARY.md` written and the remaining steps left `PENDING`, so a relaunch after
the window resets picks up exactly where it stopped. The signal is deliberately
"failed AND committed nothing": a step that fails its gate having committed real
work is a code problem and the existing retry is right for it, whereas a run of
steps that produce nothing at all is a run whose environment has gone away.

**What it must not do.** It must not try to identify a usage limit specifically -
parsing an error string for a quota message would be brittle and would miss the
other ways an environment can vanish (the CLI logged out, the network gone, a
model id withdrawn). The threshold is the whole mechanism.

**What it costs.** Small: a counter in the loop, a stop reason, and a self-test
fixture where the fake worker fails N times in a row with no commit. The fixture
is the reason it was not built on the night it was diagnosed - putting untested
runner code under an unattended run is the trade the hard rules exist to refuse.

## 10. Park and resume, rather than stop, when the usage window is the problem

**Builds on item 9, and finishes it.** Item 9 is the detection: notice that
nothing can succeed and stop marching through the plan. Stopping is the safe
answer, not the right one - the operator still wakes to a run that did a fifth of
its work and sat idle for six hours. The same signal deserves a better response:
PARK, poll, and carry on when the quota comes back.

**The behaviour.** On tripping the breaker the run does not end. It logs that it
is parked, waits ~30 minutes, and probes. When the probe succeeds it resumes at
the step it was on - the plan is the ledger, so "resume" is what this runner
already does on every launch, and nothing new has to be remembered.

**The probe must be cheap.** Retrying the real step is the obvious test and the
wrong one: a build brief is thousands of characters, it writes a fresh cache
prefix, and a failed probe every half hour all night is a bill for nothing. The
probe should be the smallest possible worker - a trivial prompt, no tools, a
one-word answer - so that testing "is the account alive" costs a rounding error.

**The heartbeat must keep beating.** A parked run and a hung run look identical
from outside, and this project has already learned that lesson once. While parked
it must log on a cadence - parked since HH:MM, next probe at HH:MM, N probes so
far - so that `/overnight progress` and a `tail -f` both show something moving.

**The open question is the clock, and it is a real one.** `--hours` exists so the
run is not still going when the operator wakes up; it is a wall-clock promise, not
a compute budget. So parked time should almost certainly NOT extend `stop_at` -
but then a three-hour park silently eats most of a seven-hour run, which is
exactly the outcome the operator was trying to avoid by scheduling around the
window in the first place. Neither answer is obviously right. A cap on total
parked time, after which the run ends normally with `SUMMARY.md` written, is
probably the honest middle - and whichever is chosen, the summary must say how
much of the night went to waiting rather than working, or the efficiency figures
lie about what the run cost per hour.

**One consequence worth stating.** A parked run still holds its `.lock`, which is
correct - it has not finished - but it means a scheduled relaunch fired at it
while parked will refuse. Parking makes the scheduled-launch pattern redundant
rather than complementary: once this exists, the right move is one long run that
sleeps through the wall, not a chain of runs timed around it. That is the point -
the timing arithmetic done by hand on 2026-09-06 stops being necessary.

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
