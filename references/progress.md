# PROGRESS - read a run, in flight or finished

The user wants to know what is happening, or what happened. Report facts from the
run's own files. Do not guess at a step's state from how long it has been quiet.

---

## 1. Get the digest

From the project root:

    python <skill dir>/overnight.py --progress

It needs no spec and no arguments. The outcomes come from the **plan file**,
which is the ledger - every step carries a `done:` block written and committed as
the run went - and the run directory supplies the log and the artefacts. It
prints:

- the run name, whether it is IN FLIGHT or FINISHED, and the self-reported cost;
- a note if the run has no git undo;
- a count by outcome, then every step with its outcome, minutes and note,
  with `<-- NEEDS YOU` against anything the operator must decide;
- which step is currently running, if any;
- every `discarded-commits.md`, `remediation.md` and `verdict.json` written;
- the steps still to run;
- the last six lines of `run.log`.

`--run <name>` picks a specific run directory if there are several.

If anything is flagged STUCK or HALTED, say so first and plainly: that plan
cannot go further until the user resolves it, and `references/blocked.md` is the
procedure. Do not offer to resume past it.

## 2. Read the things the digest only points at

The digest lists paths; it does not read them. For anything flagged, open it:

- **`verdict.json`** - a review's typed verdict. Report the verdict, the count of
  blockers and majors, and the summary line. Findings matter more than outcomes.
- **`remediation.md`** - written after a step failed twice. It says what the
  diagnostic thought was wrong. Worth reading even if the third attempt passed.
- **`discarded-commits.md`** - commits a reset threw away, each recoverable by
  the tag it names. If any is marked **FOREIGN**, somebody committed to the
  branch during the step: say so prominently, because the step was halted rather
  than resetting over it and the work is still there to be sorted out.
- **`quarantine/`** - untracked files moved aside instead of deleted, each with
  `.quarantined` appended to its original path.
- **`overnight/DECISIONS-PENDING.md`** - the questions the run could not answer.

## 3. Report it

**If the run is in flight**, lead with: which step is running, for how long
against its estimate, how many steps remain, and whether anything so far needs a
decision. Keep it short - the user is checking in, not debriefing.

**If it has finished**, lead with the shape of the night in one sentence (how
many passed first time, how many stuck, how many reviews found something), then
the things needing a decision, then the estimate-against-actual for any step that
ran well over. Put every question from the decisions file to the user as a
decision: one line, the option you would take, and why.

Then say the thing the digest cannot: **a gate is a floor, not a standard.** If
the user wants to know whether the work is any good, that is reading the commits,
and it is worth offering.

## 4. When there is nothing to read

`--progress` says so plainly and exits. Check that you are in the right project -
run output lives under the project the run was launched against, never under the
skill directory. If the user expected a run and there is none, the most likely
causes are a launch that never happened, a launch from the wrong working
directory, or a spec whose `run.name` differs from what they remember.
