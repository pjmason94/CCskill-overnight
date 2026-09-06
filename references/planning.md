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

Then say back, in five lines, what you understand the state to be. A wrong
reading surfaces here for free or at 03:00 for real money.

## 2. Interview the user

Batch the questions; do not ask serially. What you need:

- **What should be true in the morning?** Push for deliverables, not areas.
- **What must NOT be touched?** Modules, files, public interfaces, data.
- **How many hours?** This sets `--hours`, which stops new steps STARTING.
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

Give `expected_min` on every build step - it is recorded beside the actual and is
how the next plan gets calibrated. Set `timeout_min` at roughly **2.5x** the
estimate, never at the estimate: killing a worker at its expected time destroys
the work that was about to be committed.

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
   sticks) and therefore the `--hours` to pass;
3. the rough cost, if the project has past runs to calibrate against;
4. the git warning again, if it applied;
5. **that stage 1 is done and nothing has been launched.** Tell the user to come
   back with `/overnight run` when they are ready.

Do not run the self-test, dry-run or launch here. That is stage 2.
