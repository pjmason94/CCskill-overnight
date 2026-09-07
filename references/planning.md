# Stage 1 - PLAN

Write a steps spec and its briefs into **the project the user is working in**.
Nothing here goes near the skill's own directory.

Stop at the end of this file. Launching is stage 2 and may be days later.

---

## 0. Establish where you are

Confirm the project root - the directory the user's session is rooted in, not the
skill directory. Every path below is relative to it. Say it out loud in one line
so a wrong guess is caught before anything is written.

Then check git, because it changes what the run can promise:

    git rev-parse --show-toplevel
    git --version

**If either fails, say this loudly, before anything else** - a heading of its
own, not a footnote:

> **This project has no git undo.** The runner's whole safety model is that a
> failed gate resets the tree to where the step began. Without a repository that
> cannot happen: a worker running at `bypassPermissions` will leave whatever it
> wrote on disk, `clean_tree` gates are skipped, and review steps are skipped
> because there is no commit to read. The run still works - gates still gate and
> workers still work - but an overnight run of unattended agents against an
> unversioned tree is a real risk. `git init` and one commit removes it entirely.

Offer `git init` and a first commit. If the user declines, proceed - it is their
call - and note it in the spec's preamble so the workers know there is no undo.

## 1. Read the project before planning anything

A run is only as good as its plan, and the plan is only as good as the reading.
Read, in this order:

- the project's `CLAUDE.md` and any nested ones;
- the most recent session log or work journal, if the project keeps one;
- `overnight/DECISIONS-PENDING.md` from any previous run - open questions there
  are the first candidates for this run's steps;
- the test layout: how tests are named, how one is run by node id, what the
  whole-suite command is. Every gate is one of these.
- **the ledger of any previous run** - the `done:` blocks in `overnight/steps.yaml`
  and the estimate-vs-actual table in `overnight/runs/<name>/SUMMARY.md`. This is
  the only honest calibration that exists: what steps in THIS project, at these
  tiers, actually took. Use it in section 3 rather than estimating from nothing.

Then say back, in five lines, what you understand the state to be. A wrong
reading surfaces here for free or at 03:00 for real money.

## 2. Interview the user

Batch the questions; do not ask serially. What you need:

- **What should be true in the morning?** Push for deliverables, not areas.
- **What must NOT be touched?** Modules, files, public interfaces, data.
- **When will you be back at the desk?** This sets `--until`, the time after
  which no new step is STARTED. Ask for the time, not a number of hours - the
  time is what they know, and a duration written now is wrong by however long
  it takes them to launch it.
- **Which decisions may the run take, and which must wait?** Anything the run
  may not decide becomes an instruction to write to the decisions file and move
  on, never a guess.
- **Any known-fragile ground** - a flaky test, a slow suite, a generated file.

## 3. Cut the work into steps

**One deliverable per step, expected around fifteen minutes.** This is the rule
that matters most. A step carrying five deliverables ran 37 minutes and cost
twelve times a normal step when a gate reset it. Scope at plan time; the clock is
the symptom, not the disease.

- If a step's description needs the word "and", it is two steps.
- Order so nothing depends on a step that might be STUCK. What can be
  independent, make independent - a stuck step then skips rather than blocks.
- A **review** step after every build step that changed a design or touched code
  outside its own module. A gate is a floor; the review is the standard.
- A **reflect** step after every block of three to five, so the plan can respond
  to what was learned.
- The **proving case belongs inside the step**. A step whose exit is "its tests
  pass" has not exited until something real has been pushed through it.

**Calibrate against what the project has actually recorded, not against a feel
for the work.** If a previous run left a ledger, put the new steps beside the old
actuals: a step of about this size in this project took about this long. Two
things to look for, and the second is the one that pays:

- steps whose actual came in near their `expected_min` - that is what a
  correctly-cut step looks like here, and the new steps should resemble them;
- steps that ran two to ten times their estimate, or that carry no `expected_min`
  at all. Measured over a real 36-step run: every build step that carried an
  estimate finished in 8 to 24 minutes, while every step that carried none ran
  28, 33, 52, 54, 53, 119 and 151. Writing the estimate did not make them
  shorter - it is that a step nobody sized is a step nobody scoped. **An unsized
  step is the reliable predictor of an overrun**, so give every build step an
  `expected_min`, and treat any step you cannot put a number to as one you have
  not yet cut.

**Re-cutting an existing plan.** When steps that have already run are being
replaced - a block that overran being split into smaller ones - give the new
steps NEW ids. A step's outcome lives in its own block, so re-cutting under a new
id leaves the stale `done:` behind with the step it belonged to, and the new
steps start clean. Do not reach for `--reset-state` to achieve this: it forgets
every outcome in the plan, including the ones worth keeping. Steps that are
genuinely unchanged keep their ids and their history.

Propose a **tier per step** and say it: mechanical work with one obvious approach
at Sonnet medium; a build with a narrow approach at Opus medium; review, reflect
and diagnostic at Opus high. Never the top tier headless.

## 4. Write the gates

Every exit criterion that is a claim about behaviour becomes a **named test node
id**, identical in the brief and in the step's gates. The worker writes the test;
the gate runs it.

Universal gates go in `run.gates` and are appended to every build step: the whole
suite, any checksum or lint the project keeps, and `{clean_tree: true}`.

Gate forms are in `references/spec-format.md`. A gate is a command that exits 0
or not - never a judgement.

## 5. Write the files

Create, in the project:

    overnight/steps.yaml
    overnight/briefs/_preamble.md
    overnight/briefs/<step-id>.md      one per build step
    overnight/briefs/_reflect.md       optional

`examples/` beside this file is a complete fictional package to copy the shape
from. `python <skill dir>/overnight.py --format` prints the spec reference.

**The preamble** carries what every worker needs: that it is unattended, that it
commits its own work **by explicit path and never `git add -A`**, that findings
and questions go to `overnight/DECISIONS-PENDING.md` **as they are learned and
not at the end**, and what it must do when it is stuck rather than guess.

**Each brief** names the two or three files to read and what is wrong with them
today, states the one deliverable, names the exact test node id, and says what
the step must **not** build or touch. Point at documents; do not restate them.

## 6. Ignore the run's output

Add to the project's `.gitignore` if not already there:

    overnight/runs/
    overnight/DECISIONS-PENDING.md

The plan is committed; the output is not. This is what lets a finding survive a
reset that destroys the code.

## 7. Estimate, and hand over

`expected_min` is REQUIRED on every build step. The runner refuses to load a
plan with an unsized build step still to run, naming every one of them, so this
is not advisory: a plan that omits it does not launch. It is how the clock
decides whether a step can still be started, it is recorded beside the actual so
the next plan gets calibrated (section 1), and a step you cannot put a number to
is a step you have not finished cutting - split it until you can. Set `timeout_min` at roughly **2.5x**
the estimate, never at the estimate: killing a worker at its expected time
destroys the work that was about to be committed.

**A budget cap is optional, and it is all-or-nothing which way you use it.**
Leave it unset, or set `run.budget_usd_per_step` well above a good step and leave
`continuations` at 0, and a trip means something went wrong. Alternatively give
each step its own `budget_usd` at about what that step should cost and set
`run.continuations` to 1 or 2, so a step too big for one worker is handed to the
next with its work intact rather than failed. Do NOT set a tight cap without
continuations: that combination stops a step the first time it overruns, which
costs the night. If the user has not asked for cost control, set neither.

**Say where everything was written, as links the user can click.** Not for
sign-off - they are not required to approve it - but nobody should have to go
looking for a file you just created. List every file written, as a markdown link
with a path relative to the workspace root, each with a few words on what it is:

    Written to [overnight/steps.yaml](overnight/steps.yaml) - 9 steps, 5 build.
    Briefs:
      - [overnight/briefs/_preamble.md](overnight/briefs/_preamble.md) - the rules every worker gets
      - [overnight/briefs/2a-parser.md](overnight/briefs/2a-parser.md) - step 2a
      - ...

Lead with the spec: it is the one file worth a minute's reading before a night
of unattended work, and it is where a mis-scoped step is cheapest to catch.

Then state, in this order:

1. the number of steps by kind, and the tier of each;
2. the estimated wall-clock (about 25 minutes per build step on a first run,
   about 10 per review, plus up to three attempts and a diagnostic where a step
   sticks) and therefore whether the plan fits before the `until` they gave;
3. the rough cost, if the project has past runs to calibrate against;
4. the git warning again, if it applied;
5. **that stage 1 is done and nothing has been launched.** Tell the user to come
   back with `/overnight run` when they are ready.

Do not run the self-test, dry-run or launch here. That is stage 2.
