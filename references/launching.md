# Stage 2 - RUN

The plan already exists at `<project>/overnight/steps.yaml`. This stage proves
it, hands the user a launch command, and reads the results in the morning.

Read the spec and the briefs off disk. Do not reconstruct them from the
conversation - the point of the two stages is that this one works after a
`/clear`, a crash or a night's sleep.

---

## 1. Read the plan back

    python <skill dir>/overnight.py --spec <project>/overnight/steps.yaml --list

This prints every step, its kind, its title, and the outcome recorded against it
in the plan file - because **the plan file is the ledger**: after every step the
runner splices a `done:` block into that step and commits it. Steps still to run
are marked `<- to run`.

Check it against what the user expects; a plan written days ago may have been
overtaken. If steps show as already complete and the user wants the whole thing
again, that is `--reset-state` - which strips every `done:` from the plan, commits
that, and exits without launching anything. Say so explicitly before using it.
The launch is the separate command below, and stays the user's to type.

## 2. Note the git state, once

    git rev-parse --show-toplevel

If this fails, say **once**, plainly, and then move on - the user was warned
loudly at plan time and does not need it repeated at every step:

> No git undo in this project: a failed gate cannot reset the tree, review steps
> will be skipped, and whatever a worker writes stays on disk. The runner will
> log the same warning once and carry on.

The runner detects this itself and degrades. It does not need a flag.

## 3. Self-test the runner

    python <skill dir>/selftest.py

**It must print `SELFTEST PASS`.** If it does not, stop and fix the runner - do
not launch. A runner path that has not been exercised is a path that will be
exercised for the first time at 03:00, unattended.

It takes **fifteen to twenty minutes** - say so before starting it, so the wait
is not mistaken for a hang. `--only` and `--from` run part of the suite while working on
the runner, but a partial run prints `SELFTEST PARTIAL OK`, never
`SELFTEST PASS`, and nothing partial may precede a launch.

## 4. Dry-run the plan

    python -u <skill dir>/overnight.py --spec <abs path>/overnight/steps.yaml --dry-run --only <first step>
    python -u <skill dir>/overnight.py --spec <abs path>/overnight/steps.yaml --print-brief <first step>

The dry run exercises the orchestration and the gates without spending a worker,
and it writes nothing a real run would read: no `done:` block, no commit, and its
own log under `overnight/runs/<name>/dry-run/`. **Every named gate is expected to
FAIL** - no worker has written the file the gate tests - so read the failures as
"the gate ran and said what it says", not as a problem. What a dry run actually
proves is that the plan parses, the briefs resolve, and each gate command exists
and executes. It stops after one attempt per step and records nothing; the step
is reported as `DRY RUN`, never `STUCK`.
`--print-brief` shows exactly what the worker will be sent, preamble and all -
read it as the worker would, with no memory of this conversation. If it does not
stand alone, fix the brief now.

## 5. Commit everything

The runner refuses to start on a dirty tree, because `git reset --hard` is its
undo and would destroy uncommitted work. Commit the spec and the briefs. Check
that `overnight/runs/` and `overnight/DECISIONS-PENDING.md` are ignored.

## 6. Estimate the runtime and say it

About 25 minutes per build step on a first run, about 10 per review; a step that
sticks costs up to three attempts plus a diagnostic. Set `--until` to the time
the user will be back, so the clock stops **starting** new steps before then - a
step already running is never interrupted by it. `--until 07:30` means the next
07:30, so it reads correctly whether it is typed at 22:00 or at 02:00; prefer it
to `--hours`, whose arithmetic is done once and is already stale by the time the
user pastes the command.

## 7. Hand over the launch command

**Do not launch it yourself.** The workers need `bypassPermissions` and the
auto-mode classifier blocks that from inside a session, which is correct. Give
the user a command for their own shell, and match their shell exactly.

PowerShell (Windows), one line, absolute paths:

    python -u "<skill dir>\overnight.py" --spec "<project>\overnight\steps.yaml" --until 07:30

POSIX:

    python -u <skill dir>/overnight.py --spec <project>/overnight/steps.yaml --until 07:30

**`--spec` must be an ABSOLUTE path.** A relative one resolves against the
launching shell's working directory, and if that is not the project the runner
finds no repository above it and degrades - or runs against the wrong tree.

Tell them how to watch it:

    Get-Content <project>\overnight\runs\<name>\run.log -Wait      # PowerShell
    tail -f <project>/overnight/runs/<name>/run.log                # POSIX

A heartbeat line every minute carries the elapsed time and the attempt log's
size, so a hang is distinguishable from progress.

## 8. Tell them what not to do while it runs

- Do not commit to the branch, or edit tracked files, from another window. The
  runner refuses to reset over a foreign commit and will HALT the step rather
  than destroy it - which is the safe outcome, but it costs the step.
- Do not run the test suite or anything CPU-heavy alongside it. Two jobs on the
  same cores both finish later and every timing taken under contention is
  meaningless.
- Do not run two runners on one repository. There is no lock.

## 9. In the morning

Read in this order, and do not skip to the end:

1. `overnight/runs/<name>/SUMMARY.md` - the table, and the estimate against the
   actual for each step;
2. every `verdict.json` under a review step;
3. every `remediation.md` - each one is a step that failed twice;
4. every `discarded-commits.md` - work a reset threw away, still recoverable by
   the tag it names;
5. `overnight/DECISIONS-PENDING.md` - the questions the run could not answer;
6. **the commits themselves. A gate is a floor, not a standard.**

`/overnight progress` does steps 1 to 4 mechanically. It does not do step 6, and
nothing replaces it.

Then write the session log, and put the decisions to the user as decisions - one
line each, with the option you would take and why.
