# overnight

An unattended runner for headless Claude Code workers. You write a plan of steps;
it executes them one at a time against a git repository, gates each one on commands
you choose, reviews the ones that matter, and can rewrite its own remaining plan as
it learns. Git is the undo underneath all of it.

It is a Claude Code **skill**: install it once, point it at a steps file, and leave
it running.

## Contents

1. [What it is for](#1-what-it-is-for)
2. [Install](#2-install)
3. [Quick start](#3-quick-start)
4. [How a run works](#4-how-a-run-works)
5. [Git: what it needs and what it does](#5-git-what-it-needs-and-what-it-does)
6. [Working alongside a live run](#6-working-alongside-a-live-run)
7. [The steps file](#7-the-steps-file)
8. [Briefs](#8-briefs)
9. [Command line](#9-command-line)
10. [Outputs](#10-outputs)
11. [Watching a run](#11-watching-a-run)
12. [What a worker is given](#12-what-a-worker-is-given)
13. [The self-test](#13-the-self-test)
14. [Costs and calibration](#14-costs-and-calibration)
15. [Judgement calls](#15-judgement-calls)
16. [Limitations](#16-limitations)
17. [Troubleshooting](#17-troubleshooting)
18. [Files in this repository](#18-files-in-this-repository)
19. [Changelog](#19-changelog)
20. [Licence](#20-licence)

## 1. What it is for

**The problem.** An interactive Claude Code session is bounded by two things: the
person at the keyboard, and the context window. A long piece of work - a stage of
a build plan with twenty deliverables, a migration touching thirty files, a
backlog of well-specified fixes - needs more hours than anyone wants to sit
through, and more context than one window holds. Left to run on its own, a single
long session drifts: its context fills with the debris of earlier steps, a wrong
turn at step 4 is built on by steps 5 to 20, and there is nobody awake to say stop.

**The shape of the answer.** The orchestrator is a Python script, not a model, so
its context does not climb over a night. Each deliverable goes to a fresh worker
with a fresh context and one job. Success is decided by commands that exit 0 or
not - never by the worker's opinion of its own work. A wrong turn is caught by a
gate, undone by git, diagnosed, and retried; if it is still wrong the step is
marked stuck and the run moves on rather than building on it. A read-only
reviewer holds the passed steps to a standard higher than the gates. Every finding,
question and dead end is written to a file as it is learned, so nothing that
matters lives only in a context window.

**Use it when:**

- the work divides into steps of roughly fifteen minutes each, and you can say
  for each one what "done" looks like as a command;
- the project has tests, or you are willing to write the test that encodes each
  step's contract before the run;
- you will not be there - overnight, over a weekend, during a day of meetings;
- the cost of a wrong step is bounded by git, because the work is in a repository.

**Do not use it when:**

- the work is one long judgement rather than many bounded deliverables (a design,
  a review, a negotiation with a spec) - do that interactively;
- "done" cannot be written as a command - a gate that is a matter of taste is a
  gate that always passes;
- the steps depend on decisions nobody has made - the runner will not make them,
  and a chain of interim guesses compounds (see section 15);
- there is no repository and no way to make one - it will run, but without an
  undo (section 5).

The first two real runs were over a financial-modelling codebase: twelve steps
and twelve first-attempt passes in 4.6 hours the first night; then a longer run
that exercised the failure paths properly and produced most of what is in this
manual.

## 2. Install

**Requirements.** Python 3.9 or later with `pyyaml`; the `claude` CLI on PATH and
logged in (a subscription login - see the note on billing in section 12); git on
PATH and a repository to run in (recommended, not required - section 5). Developed
and used on Windows 11; the POSIX branches exist and are described in section 16.

**Claude Code 2.1.242 or later is strongly recommended**, because that is the
first version in which the prompt-cache TTL can be set at all
(`promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL`). That setting is worth about
6.5% of a run's tokens - two-thirds of everything a worker writes to cache is
written seconds after the last write, so it never needs an hour's lifetime and
pays 2x for one. Below 2.1.242 you cannot change it, and cannot correct it when
the default moves under you (a subscription in overage silently drops to 5
minutes). See section 14 and `docs/token-efficiency.md`. **The runner spawns
whatever `claude` is on `PATH`**,
which is not necessarily the version your editor is running, so check the one
that matters:

    claude --version

**1. Clone this repository** somewhere permanent. It is the source of truth: the
skill directory will be a link to it, not a copy.

    git clone <this repository> overnight
    cd overnight

**2. Install the skill.** Claude Code looks for skills in
`<scope>/.claude/skills/<name>/SKILL.md` and that path is not configurable, so the
installer makes that directory a link back here - a directory junction on Windows
(no administrator rights needed), a symlink elsewhere. Real files stay under
version control; the harness reads the path it expects; an edit in either place
is the same edit.

    python install.py                 # user scope:    ~/.claude/skills/overnight
    python install.py --project .     # project scope: <dir>/.claude/skills/overnight
    python install.py --check         # report what is installed, change nothing
    python install.py --force         # replace whatever is there already
    python install.py --uninstall     # remove the link; the checkout is untouched

`--copy` copies instead of linking, for a filesystem that cannot link. Avoid it:
a copy drifts silently from the checkout, and a fix made here never reaches the
skill that is actually loaded. If you must, re-run `install.py` after every
change.

**3. Run the self-test.** It drives every path of the runner against a fake
worker in a throwaway repository, in under a minute, for no tokens.

    python selftest.py

It must print `SELFTEST PASS`. If it does not, nothing else in this manual is
worth trying until it does.

**4. Start a new Claude Code session** so the skill list is re-read. `/overnight`
is then available in every project (user scope) or in that project (project
scope).

## 3. Quick start

Everything an overnight run needs, and everything it writes, lives under
`<project>/overnight/`:

    <project>/
      overnight/
        steps.yaml              the plan - committed
        briefs/
          _preamble.md          rules every worker gets - committed
          <step-id>.md          one brief per build step - committed
        DECISIONS-PENDING.md    questions the workers raise - gitignored
        runs/<run-name>/        everything the run writes - gitignored

Add to the project's `.gitignore`:

    overnight/runs/
    overnight/DECISIONS-PENDING.md

The plan and the briefs are committed because the runner needs a clean tree to
start, and because a reflect step commits its rewrite of the plan. The run
directory is ignored because a worker's commit must never carry the run's logs,
and a reset must never destroy a finding.

**Prove the loop before spending a token.** `examples/` is a complete plan for a
project that does not exist, and one command runs it end to end against the fake
worker in a scratch repository:

    python examples/try_it.py

You will see a build pass and commit, a review return `rework` and the rework
land on top of the reviewed commit, a build fail its gate and pass on the retry,
a reflect decide the plan needs no change, and the digest and `SUMMARY.md` at the
end. `--keep` leaves the scratch repository behind to poke at.

**Write the real plan.** In the project, `/overnight` walks through it: read the
project's state, draft `overnight/steps.yaml` with one deliverable per step, write
the briefs, self-test, dry-run, commit, estimate, launch. Or do it by hand with
section 7 and section 8.

**Check the plan without workers.**

    python -u overnight.py --spec <absolute path>/overnight/steps.yaml --list
    python -u overnight.py --spec <absolute path>/overnight/steps.yaml --print-brief <step id>
    python -u overnight.py --spec <absolute path>/overnight/steps.yaml --dry-run --only <first step id>

None of these three writes an outcome into the plan or commits anything, and
`--list` and `--print-brief` create nothing at all - not even the run directory,
because a `run.log` is what `--progress` reads as "a run started here". The dry
run does execute each gate command, and every named gate is expected to fail -
no worker has written what it tests yet. The point is that the gate ran at all.

**Commit everything.** The runner refuses to start on a dirty tree, because
`git reset --hard` is its undo and would destroy uncommitted work.

**Launch** from a terminal in the project, and give `--spec` an **absolute**
path: a relative one resolves against the launching shell's working directory,
and if that is not the project the runner will not find the repository.

    python -u <path to overnight.py> --spec "<absolute path>/overnight/steps.yaml" --hours 8

Windows PowerShell:

    python -u C:\Users\<you>\.claude\skills\overnight\overnight.py --spec "C:\path\to\project\overnight\steps.yaml" --hours 8

Launch it yourself, from your own shell. Claude Code's permission classifier will
(correctly) refuse to let a session spawn workers at `bypassPermissions` on your
behalf.

**Morning.** Read `overnight/runs/<name>/SUMMARY.md`, then every `verdict.json`
and `remediation.md`, then `DECISIONS-PENDING.md`, and then the commits
themselves. A gate is a floor, not a standard.

## 4. How a run works

The runner reads the plan, runs a preflight, and then takes the steps in order.
Before every step it re-reads the plan from disk (a reflect step may have
rewritten it), skips anything already completed, and checks the clock. When the
queue is empty, the clock has run out, or the plan has become invalid, it writes
`SUMMARY.md` and exits.

**Preflight.** The working tree must be clean and every universal gate (the
`run.gates` list) must pass before any step starts. Either failing is a refusal,
exit code 2, with the reason in `run.log`. A gate that fails before the first
worker has touched anything is a broken plan, not a broken worker.

**The clock.** `--hours` (or `run.hours`) is the time after which no new step is
STARTED. A step already running finishes, including its gates and any review that
follows. Set it so the last step can start before you are back, not so the run
ends exactly then.

### Build steps

A worker is spawned with the brief on stdin (section 12), works in the repository
with `bypassPermissions`, and is expected to commit its own work by explicit path.
When it exits, the runner runs the step's gates - its own, then the universal
ones - from the repository root.

- **All gates pass:** PASS. HEAD is recorded as the step's commit. If HEAD did not
  move the pass is recorded with the note "gates pass but HEAD did not move",
  which usually means the step was already done or the worker forgot to commit.
- **A gate fails:** the failure is logged with the gate's name, the repository is
  reset to where the attempt began (section 5 says exactly what that protects),
  and the next attempt starts with a fresh worker and the same brief.
- **After the second failure**, a diagnostic worker reads a compact transcript of
  both attempts (the assistant's own words and every tool error, extracted from
  the stream - tens of kilobytes rather than megabytes) and their gate output, and
  writes `remediation.md`: what went wrong on each, whether they share a root
  cause, what the third attempt must do differently, what it must not retry, and
  the smallest useful subset if the step as briefed is not achievable. The third
  attempt's brief carries that plan.
- **Still failing after `attempts` tries:** STUCK. The run moves on. A STUCK step
  is re-run on the next launch (section 9, resume).
- **The worker is killed** if it exceeds `timeout_min` (default
  `run.worker_timeout_min`, 90); that counts as a failed attempt.

### Review steps

A read-only worker (Edit, Write and NotebookEdit disallowed) is given the passed
commit of the step it reviews (`of:`), the brief that step was given, the worker's
own summary, and any extra guidance file. It judges against the brief, the
project's CLAUDE.md, correctness (a test that cannot fail, a guard never shown to
bite, a rule relaxed to make a result come out) and honesty (does the commit say
what it did not build), and returns a typed verdict through a JSON schema:
`pass`, `rework` or `fail`, with findings graded `blocker`, `major` or `minor`
and, for rework, a brief a fresh worker can follow. Minor findings alone are a
pass. The runner resets the tree after it regardless.

What happens on `rework` or `fail` is the step's `on_fail`:

- `record` - write `verdict.json` and move on.
- `rework` (the default) - run ONE more build attempt on top of the reviewed
  commit, with the findings appended to the original brief. If it passes its
  gates, the step's commit is updated and the review is recorded REVIEW REWORK
  PASS. If not, the reviewed commit stands and the review is recorded REVIEW
  REWORK FAILED.
- `revert` - reset to the commit before the reviewed one; the reviewed step
  becomes REVERTED BY REVIEW.

A review is skipped if the step it reviews did not PASS, and recorded
INCONCLUSIVE if the worker returned no typed verdict.

### Reflect steps

A worker reads how the run has gone - the outcome table, the pending steps, the
tail of `run.log`, the decisions file - and may edit the plan: pending steps only.
It may edit, remove, reorder or insert them, and add brief files beside the
existing ones. Grounds for a change are that a finding makes a later step
pointless or wrong, a decision blocks a later step and an interim is possible, a
stuck step should be split or dropped, or a defect found in passing deserves its
own step. Not: making the plan easier.

The runner then validates the rewrite and reverts it from a backup on any
violation: a completed step changed, removed or reordered; the `run:` section
changed; a new build step naming a brief that does not exist; invalid YAML; any
file touched outside the plan and its briefs. A valid change is committed with the
reflect's rationale as the message. A reflect that changes nothing is recorded as
such.

### Gate steps

Commands only, no worker. For a long measurement whose result is a number in a
file, or a checkpoint you want recorded between builds.

## 5. Git: what it needs and what it does

**Git is recommended, not forced.** Put every project you run this over in a
repository - `git init` is enough - because git is the undo. Without it the runner
still works, but degraded (below).

**What the undo does.** HEAD is read before every attempt. A failed gate resets the
tree to it (`git reset --hard`, then `git clean -fd` - never `-x`, so ignored
directories such as `overnight/runs/` and any data or corpus you keep ignored are
never touched). This is what makes `bypassPermissions` safe: the worker can do
anything to the repository, and anything it does wrong is undone.

**What the undo protects, and how.** The runner does not own the branch for the
hours a run lasts. You, another Claude window, an IDE or a hook can commit at any
point inside a step, and a naive reset to a baseline taken at step entry would
remove that commit silently - a branch that has quietly lost a commit looks
exactly like a branch that never had one. So:

- Every worker commits under a run-specific **committer** identity
  (`overnight worker (<run name>)`, `overnight+<run name>@runner.invalid`),
  inherited through the environment and needing no cooperation from the worker.
  The **author** is left as the project's git config sets it, so the history
  reads normally. This is what lets a reset tell the run's commits from anyone
  else's as a fact rather than a guess.
- Before any reset, every commit it would discard is **tagged**
  (`rescue/<step>-<attempt>/<n>`), **logged** with its subject in `run.log`, and
  listed with recovery instructions in the step's `discarded-commits.md`. A
  worker's own work that a gate rejected is therefore one `git cherry-pick` away,
  not a reflog search.
- If any commit in the range was made by **somebody other than the run's
  workers**, the reset is **refused**, the tree is left as it is, and the step is
  recorded HALTED. Refusing is a perfectly good outcome; destroying someone
  else's work is not. The same refusal applies to a review's `revert`.
- Untracked files that `git clean` would delete are **moved** into the step's
  `quarantine/` directory (with `.quarantined` appended, so a rescued test file is
  not collected by the next attempt's test run) and listed in a `MANIFEST.txt`,
  never deleted.

**What is not protected.** Uncommitted edits to TRACKED files made by anyone
during a step. `git reset --hard` discards them, there is no identity on an
uncommitted change to tell whose it was, and nothing on disk records that they
existed. See section 6.

**What the runner commits.** Only a reflect step's plan change, with the
rationale as the message. Workers commit their own work. Nothing pushes.

**Without git.** If there is no repository at or above the spec, or git is not on
PATH, the run is DEGRADED, not refused: it says so once at the top of `run.log`
and again in `SUMMARY.md`. A failed gate cannot reset the tree, so a worker's
edits stay on disk and must be undone by hand; `clean_tree` gates are skipped;
review steps are skipped for want of a commit to read; a reflect step's rewrite is
not committed. Gates and workers otherwise run normally. Use this for a
throwaway; do not use it for anything you would mind losing.

## 6. Working alongside a live run

The runner is designed for a night with nobody there, and is built to be fairly
robust to someone being there anyway. It shares the repository with you.

**Isolation is the default, and it changes this section.** Unless the spec says
`run.isolation: in-place`, each build step works in its own git worktree on a
scratch branch - so nothing below about the worker and your tree colliding
applies to build steps at all. Your tree is not the worker's tree; the work
arrives on your branch in one integration step after its gates have passed; a
commit of yours mid-step is replayed around rather than refused; and a file you
write into your own tree while a step runs is invisible to that step's gates.
What remains true with isolation on: review and reflect steps still run in your
tree, and a step whose work cannot be replayed onto your branch is recorded
`NEEDS MERGE`, keeps its commits on its scratch branch, and stops the run for a
person to land it. The rest of this section describes `in-place`, which you get
by asking for it.

The worktrees go in a **sibling** directory - `<repo>.overnight-worktrees/` - and
never inside the repository. That placement is load-bearing twice over: a
worktree under `overnight/runs/` would be walked by any universal gate that
collects from the project root (`pytest -q` reads no `.gitignore`), and it would
itself show up as an untracked path in the very tree whose cleanliness it exists
to protect.

A second *runner* is refused either way - the lock (section 16) holds the
repository. What it does not lock is you. What that means in practice:

**Safe.**

- Reading anything. Watching `run.log`. Reading the attempt logs.
- Working in a different repository.
- Writing under an ignored directory - the run directory, a scratch directory the
  project ignores. That is where the runner itself puts findings, for this reason.
- Committing to the branch. A worker's commit lands on top of yours; if a later
  gate fails, the reset is refused rather than taking your commit with it, and the
  step halts. You lose a step, not a commit.
- Creating an untracked file. If it exists before a step starts, the clean-tree
  gate ignores it; if it appears during one, a reset moves it to quarantine rather
  than deleting it. (A file that appears mid-step does fail that step's clean-tree
  gate, since the runner cannot tell it from the worker's - so prefer the ignored
  directory.)

**Risky - do not.**

- **Editing a tracked file and leaving it uncommitted.** A gate failure resets it
  away with no record. Commit it, stash it, or do not touch tracked files while a
  run is live.
- **Running the project's test suite, or anything CPU-heavy, while a worker
  runs.** Two jobs on the same cores both finish later, every timing taken under
  contention is meaningless, and a gate that runs the suite may fail on a file the
  worker is half-way through writing. The runner itself never runs a gate while a
  worker is running, for this reason.
- **Editing the plan or a brief by hand mid-run.** The runner re-reads the plan
  before every step, so an edit takes effect - but a reflect step's validation
  compares against the plan it backed up, and a hand edit and a reflect edit in
  the same window will confuse each other.
- **Running the same steps file twice at once.** Two runners on one repository
  have not been tested and there is no lock to stop you. They would share
  the ledger in the plan file, reset each other's work, and each refuse the other's commits as
  foreign. Treat this as unsupported until it is.

**A note on your own Claude Code session.** Keeping an interactive session open
on the same project while a run is live is fine for reading and for work under
ignored directories. It is the most common source of the risky cases above:
an assistant that helpfully "tidies" a tracked file, or writes a session note
into the repository, does exactly what the first bullet warns against. Tell it
where the run directory is and that the tree is off limits.

## 7. The steps file

`python overnight.py --format` prints the reference form. Every key:

### `run:`

| key | default | meaning |
|---|---|---|
| `name` | required | names the output directory `overnight/runs/<name>/` |
| `hours` | 6 | stop STARTING steps after this many hours; `--hours` overrides |
| `attempts` | 3 | build attempts before STUCK |
| `worker_timeout_min` | 90 | kill a build worker after this many minutes |
| `review_timeout_min` | 30 | the same for a review worker |
| `reflect_timeout_min` | 30 | the same for a reflect worker |
| `diagnostic_timeout_min` | 20 | the same for the diagnostic pass |
| `budget_usd_per_step` | none | passed to every worker as `--max-budget-usd` (see section 14 on what that figure is) |
| `preamble` | none | a file prepended to every build brief; `{CHUNK}` in it is replaced by the step id |
| `decisions_file` | `overnight/DECISIONS-PENDING.md` | where workers write questions and findings; read by reflect steps |
| `out` | `overnight/runs` | the parent of the run directory |
| `defaults` | opus/medium build; opus/high review, reflect, diagnostic | model and effort per kind: `build: {model: sonnet, effort: medium}` |
| `gates` | none | universal gates, appended to every build step's own |

### A step

| key | applies to | meaning |
|---|---|---|
| `id` | all | unique; used as an address in logs, state and `--only`. `review:<step>` is the convention for reviews. Any character is allowed; the step's directory name has `<>:"/\|?*` replaced by `-` |
| `kind` | all | `build` (default), `review`, `reflect`, `gate` |
| `title` | all | one line, shown in logs and the summary |
| `brief` | build (required); review, reflect (optional extra guidance) | path relative to the repository root |
| `of` | review (required) | the id of an EARLIER step |
| `on_fail` | review | `record`, `rework` (default), `revert` |
| `model`, `effort` | build, review, reflect | override the kind's default tier |
| `expected_min` | any | a plan-time estimate; recorded beside the actual (section 14). Never terminates anything |
| `timeout_min` | any with a worker | hard kill; belongs at roughly 2.5x `expected_min`, never at the estimate |
| `gates` | build, gate | this step's own gates, run before the universal ones |

### Gates

A gate is a mapping with exactly one of these keys, plus an optional `name`:

| form | passes when |
|---|---|
| `cmd: <shell command>` | exit 0 |
| `cmd_empty: <shell command>` | exit 0 AND prints nothing (`git status --porcelain`, a linter) |
| `file: <path>` | exists and is not empty |
| `fresh_shell: <command>` | exit 0 in a NEW shell - on Windows a PowerShell with PATH rebuilt from the registry, on POSIX a login shell. The only honest test of "works in a new terminal" |
| `clean_tree: true` | `git status --porcelain` prints nothing (untracked files present before the step began are ignored) |

Commands run from the repository root through the shell, with a 30-minute
timeout. A test node id in a gate is a **contract**: the brief names it, the
worker creates it, the gate runs it. Write the gate before the brief.

### Tiers

`model` is whatever the `claude` CLI accepts (`sonnet`, `opus`, ...); `effort` is
`low`, `medium` or `high`. Pick the model by the ceiling the step's hardest part
needs and the effort by how many approaches must be weighed. Mechanical work with
one obvious approach - a new attribute, a flattened wrapper, a test that walks a
file - is sonnet/medium. A build with a narrow approach is opus/medium. Review,
reflect and diagnostic want opus/high: they are judgement, and cheap relative to
the build they judge.

## 8. Briefs

A brief is a markdown file the worker reads once. The composed brief a worker
receives is: the preamble, then `# Your step: <id> - <title>`, then the brief
file, then the gates rendered as a list, then (attempt 3) the remediation plan,
then (rework) the review's findings, then `This is attempt N.` See exactly what
will be sent with `--print-brief <id>`.

**The preamble** carries what every worker needs and no brief should repeat: the
project's rules, how to finish (run the gates yourself, commit by explicit path,
never `git add -A`, leave files you did not create alone), where to write a
question or a finding as it is learned (the decisions file), and what to say in
the commit message (what was NOT built, and why). `{CHUNK}` is replaced by the
step id.

**A good brief** (see `examples/briefs/first-thing.md`):

- names the two or three files to read first and says what is wrong with them
  today - a brief that describes the goal without the current state makes the
  worker rediscover it;
- has ONE deliverable. More than about three bullets under "build" is two steps.
  A step carrying five deliverables ran 37 minutes and cost twelve times a normal
  step when a gate reset it;
- says what the change must NOT do and which module it must not touch;
- names the test node id the gate will run, exactly, and where the test guards a
  defect, requires it be shown to FAIL on that defect first;
- names the failure mode you expect and the thing that means "stop and write it
  down" rather than "push on";
- points at documents rather than restating them. The worker has the repository.

## 9. Command line

    python -u overnight.py --spec <path> [options]

| flag | meaning |
|---|---|
| `--spec <path>` | required. The steps file. Give an ABSOLUTE path |
| `--repo <dir>` | the repository root. Default: found by walking up from the spec |
| `--hours <n>` | overrides `run.hours` |
| `--list` | print every step, its kind, its recorded outcome, its title; exit |
| `--print-brief <id>` | print the composed brief a build or reflect worker would receive; exit |
| `--format` | print the steps file reference form; exit |
| `--dry-run` | no workers. Preflight, then each step's gates as they stand. Proves the plan parses and the gates run. Writes no `done:`, commits nothing, logs to `runs/<name>/dry-run/`; every named gate is expected to fail |
| `--only a,b,c` | run only these steps, in plan order. Naming a **review** step can cost more than it looks: a `rework` verdict spawns a full build worker on top of the reviewed commit and re-runs its gates, and that rework is not a step you named. Budget a review at review + rework, about 1.5x the build it reviews |
| `--from <id>` | run from this step to the end |
| `--rerun` | run steps already recorded as passed |
| `--reset-state` | forget every recorded outcome; exit. Launches nothing - the relaunch is a separate command |
| `--fake-worker <script>` | substitute a script for `claude` (the self-test uses `fake_worker.py`) |

**Resume.** Outcomes are recorded **in the steps file itself**, as a `done:`
block on each step, committed as they happen. Relaunching with the same spec
skips every step that completed: PASS, a review that ran, a reflect that ran,
anything SKIPPED for a reason that will not change. It re-runs a step that did
not complete: STUCK, FAIL, HALTED, INCONCLUSIVE, REWORK FAILED, REVERTED BY
REVIEW, and a review SKIPPED because its subject had not passed. `--rerun`
re-runs passed steps too; `--reset-state` strips every `done:` from the plan,
commits that, and **exits**. It is an exclusive action, like `--list` and
`--print-brief`: forgetting a night's outcomes is a decision of its own, and
starting the plan again is the next command, typed deliberately. (Until
2026-09-05 it fell through into a live launch.)

**`--mode`** answers "what should happen next?" from the plan file alone, with no
`--spec` argument, so a script or a skill can branch on it:

| it prints | when | exit |
|---|---|---|
| `BLOCKED` | a step is STUCK or HALTED - it needs a person | 3 |
| `PLAN` | there is no plan file | 0 |
| `RUN` | steps are still to run | 0 |
| `REPLACE?` | every step completed | 0 |

BLOCKED is tested first and deliberately overrides the rest.

**Exit code.** 0 when every recorded outcome is benign (PASS, REVIEW PASS, REVIEW
REWORK PASS, REFLECT NO CHANGE, REFLECT CHANGED, SKIPPED); 1 when any is not
(STUCK, HALTED, FAIL, REVIEW FAIL, REVIEW REWORK FAILED, REFLECT REVERTED,
INCONCLUSIVE); 2 when preflight refused to start. Unknown step ids and a missing
`claude` exit 1 with a message before anything runs.

## 10. Outputs

Everything is under `overnight/runs/<run name>/`:

    run.log                       one line per event, every minute at least
    preflight.log                 the universal gates' output before step 1
    SUMMARY.md                    written when the run ends
    <step id>/                    one directory per step that ran
      summary.md                  the worker's own summary of what it did
      attempt-1.log               the worker's full stream, brief first, gates last
      attempt-2.log
      diagnostic.log              the diagnostic worker's stream (after 2 failures)
      remediation.md              what the diagnostic wrote; attempt 3 reads it
      attempt-3.log
      rework.log                  the one attempt after a review's rework verdict
      discarded-commits.md        every commit a reset discarded, with its rescue tag
      quarantine/<label>/         untracked files moved aside instead of deleted
        MANIFEST.txt
        <path>.quarantined
    review-<step id>/             a review step (the `:` in its id becomes `-`)
      review.log
      verdict.json
    reflect-1/
      reflect.log
      steps.before.yaml           the plan as it was; restored on a violation
      commit-msg.txt              the plan-change commit message
    <gate step id>/
      gates.log

### `run.log`

    2026-09-05 13:07:05  [3b-two-pass-compiler] start (build) at 8da32644 - declare then emit
    2026-09-05 13:07:05  [3b-two-pass-compiler] attempt-1: worker starting (opus/medium, 8962 chars) -> attempt-1.log
    2026-09-05 13:08:07  [3b-two-pass-compiler] attempt-1: still running, 1 min, log 10 KB
    2026-09-05 13:34:12  [3b-two-pass-compiler] attempt-1: worker exit 0 after 27.1 min | turns=61 $8.12 | <first 300 chars of the worker's summary>
    2026-09-05 13:36:40  [3b-two-pass-compiler] PASS (committed at 1f3c9a2e)

Lines to grep for: `start (`, `PASS`, `FAIL`, `STUCK`, `HALTED`, `verdict`,
`rework`, `PLAN CHANGED`, `REVERTED`, `DISCARDING`, `REFUSING`, `QUARANTINED`,
`CANNOT RESET`, `REFUSING TO START`, `STOP:`, `summary written`. The heartbeat
(`still running`) is the line to grep OUT.

### The ledger: `done:` in the steps file

There is no `state.json`. Every outcome is written into the step it belongs to,
in `overnight/steps.yaml`, and committed as `overnight: <step id> <outcome>`:

```yaml
  - id: 3b-discretion
    kind: build
    brief: overnight/briefs/3b-discretion.md
    expected_min: 15
    gates:
      - {cmd: python -m pytest -q tests/test_discretion.py}
    done:
      outcome: "PASS"            # the vocabulary below
      at: "2026-09-05 10:22"     # local time, minute resolution
      sha: "f756c08a"            # absent for SKIPPED and for a run without git
      attempts: 1                # build steps
      minutes: 29.6              # wall clock over every attempt
      cost_usd: 2.39             # this step's workers, self-reported. A review
                                 # step's figure includes any rework IT ordered,
                                 # which is what makes the overhead split honest
      tier: "opus/medium"        # what the runner ASKED for
      note: "committed at f756c08a"
      reworked: true             # on the reviewed step after a rework pass
      of: "3b-discretion"        # review steps
      findings: 3                # review steps
      added: [...]  removed: [...]   # a reflect that changed the plan
```

The run's cumulative cost is the sum of every `done.cost_usd`, and it is printed
on the run's last log line as well as in `SUMMARY.md`. The worker's own summary
is **not** in the yaml - it would make the plan unscannable - and goes to
`<step dir>/summary.md`.

`tier` is the model and effort the runner **asked for**. Do not read that off the
commit: a worker's `Co-Authored-By` trailer names whatever its own session's
attribution settings name, which has nothing to do with the `--model` it was
spawned with, and on 2026-09-06 a `sonnet/medium` step produced a commit
crediting Opus.

**The file is spliced, never re-dumped.** Comments, key order and quoting all
survive, because a `yaml.safe_dump` round trip would silently destroy them. Each
write re-parses the result and refuses any edit that would have changed another
step, so it is safe to run unattended. There is never more than one `done:` per
step: a rework or a revert replaces the block in place.

**You may edit a pending step by hand while a run is live.** The runner re-reads
the file immediately before each write and touches only the block it is
recording. Editing a step that already carries `done:` is not defended against.

Outcomes: `PASS`, `STUCK`, `HALTED`, `FAIL` (a gate step), `SKIPPED`,
`INCONCLUSIVE`, `REVIEW PASS`, `REVIEW REWORK PASS`, `REVIEW REWORK FAILED`,
`REVIEW FAIL`, `REVERTED BY REVIEW` (on the reviewed step), `REFLECT CHANGED`,
`REFLECT NO CHANGE`, `REFLECT REVERTED`.

### `verdict.json`

    {
      "verdict": "rework",                pass | rework | fail
      "summary": "...",
      "findings": [
        {"severity": "major",             blocker | major | minor
         "where": "src/x.py:212",
         "what": "...",
         "fix": "..."}
      ],
      "rework_brief": "..."               instructions a fresh worker can follow
    }

### `SUMMARY.md`

The run's name, start, end and duration; why it ended (the queue finished, the
clock, an invalid spec); HEAD; the degraded-mode warning if there is one; then a
table - step, kind, outcome, minutes, estimate with the actual-to-estimate ratio
(marked **over** past 1.5x), note - and the cumulative worker cost; then what to
read next.

The cost line is followed by **the split between building and not building**:

    Cumulative worker cost over every step this plan has run, not just this session (self-reported): $18.40
    Of that, **$4.85 (26%) was not building**: review and reflect steps, and the rework they ordered. Build steps $13.55, of which 2 needed more than one attempt and 1 was reworked after a review - that spend is inside the build figure, because the ledger keeps one cost per step.

The efficiency bar this project holds itself to (see `docs/token-efficiency.md`)
is not about what a single API call costs - headless workers cost the same per
call as an interactive session. It is structural: review, reflect, rework and
discarded attempts were 26% of the first real run. So it is reported every
morning rather than asserted in a document.

Read the two halves differently. The **kind split is exact** - a review or a
reflect is a whole step with its own ledger entry, and a rework's cost is moved
onto the review that ordered it. The **retried and reworked counts are
indicative**: the ledger keeps one cost per step, so a build step's discarded
attempts are inside its own figure and cannot be separated from the attempt that
finally worked. A high overhead share is not automatically bad - a review that
catches a real bug has earned its money - but a run where half the spend is not
building is one whose plan is asking the wrong questions.

### `discarded-commits.md`

One block per reset, appended never overwritten:

    # commits that the reset for `s2-attempt-1` would discard (gate failed)

    - `<sha>` worker <overnight+night-2@runner.invalid> tag `rescue/s2-attempt-1/1`: fake: s2 did work but missed the contract

    Recover one with `git cherry-pick <sha>`; drop the tags with `git tag -d <tag>` when done.

A line marked `FOREIGN` is a commit the run did not make; the reset that would
have discarded it was refused.

### The worker's own files

`DECISIONS-PENDING.md` (or whatever `run.decisions_file` names) is the workers'
file, not the runner's: the preamble tells them to write a question, a finding or
a dead end there AS IT IS LEARNED, because a reset destroys code but never a file
under an ignored path. It is read by every reflect step and is the first thing to
read in the morning after `SUMMARY.md`.

## 11. Watching a run

The runner prints every line it logs, so the terminal it was launched from shows
the run live. From another terminal:

    Get-Content overnight\runs\<name>\run.log -Wait          # PowerShell
    tail -f overnight/runs/<name>/run.log                     # POSIX

**What the heartbeat tells you.** Once a minute while a worker runs: the step,
the attempt, minutes elapsed, and the size of the worker's log. That is all. It
is a liveness signal - a log that is still growing is a worker that is still
working; a log that has not grown for ten minutes is a worker waiting on
something or spinning. It is not a progress bar and it will not tell you what the
worker is doing, how far through the brief it is, or whether it is going to pass.
The design accepts this: a worker's progress is not measurable from outside
without reading its stream, and reading its stream from an interactive session
costs the context that section 6 warns about.

**When you want more.** The attempt log is the worker's full `stream-json`
output, one JSON event per line, with the brief at the top and the gate output at
the bottom. It is large - one to three megabytes for a typical build step - and
mostly tool traffic. The compact form the diagnostic reads (the assistant's own
words, the tool calls, every tool error) is what to look at; there is no CLI for
it yet, but `Runner.compact_transcript(path)` is importable.

**Signals worth reacting to** (these are the lines a monitor should match):
`FAIL`, `STUCK`, `HALTED`, `REFUSING`, `CANNOT RESET`, `verdict REWORK`,
`verdict FAIL`, `REVERTED`, `STOP:`, and a heartbeat whose log size has stopped
changing.

## 12. What a worker is given

    claude -p --model <m> --effort <e> --output-format stream-json --verbose --permission-mode <mode> --disallowedTools <...> [--max-budget-usd <n>] [--json-schema <schema>]

| kind | permission mode | tools | schema |
|---|---|---|---|
| build | `bypassPermissions` | the outward-facing tools disallowed | none |
| review | `bypassPermissions` | those, and Edit, Write, NotebookEdit | the verdict |
| reflect | `bypassPermissions` | the outward-facing tools disallowed | the plan-change record |
| diagnostic | `bypassPermissions` | the outward-facing tools disallowed | none |

**No worker of any kind can reach a tool that acts outside the tree**
(`UNATTENDED_DENY` in `overnight.py`): `Artifact`, `CronCreate`, `CronDelete`,
`CronList`, `DesignSync`, `PushNotification`, `RemoteTrigger`, `SendMessage`,
`Workflow`. Each of them publishes a page, schedules or fires work that outlives
the run, messages somebody, or fans out further agents - and the git undo that
sits under everything else in this runner does not reach any of it. Nobody is
awake to see it happen and a reset cannot take it back, which is the whole
argument; that their definitions also leave the prefix every API call re-reads,
worth about 4% of a run's tokens, is a bonus and not the reason. A name the
installed CLI does not have is simply inert, so the list can safely name a tool
only some versions ship.

Reflect and diagnostic get the same permission mode as build, not the narrower
`acceptEdits` an earlier version gave them: `acceptEdits` accepts an edit but
refuses every shell command, which meant a reflect worker could rewrite the plan
and commit it but not copy a file, and a diagnostic could not run the failing
test it exists to explain. What actually fences these two is not the permission
mode - a reflect's changes outside the plan and brief directories are reverted,
and a `safe_reset` sits under both.

### The environment a worker gets

Three variables are **removed** from every child process the runner spawns -
workers, gates and git alike (`STRIP_ENV` in `overnight.py`):

| stripped | why |
|---|---|
| `ANTHROPIC_API_KEY` | so a worker authenticates as the logged-in subscription rather than billing an API account |
| `CLAUDE_EFFORT` | an inherited effort setting silently overrides the `--effort` the runner passes for the step |
| `CLAUDE_CODE_SUBAGENT_MODEL` | same, for any subagent a worker spawns |

**The API key strip is the one to know about.** Editors and shells commonly
export `ANTHROPIC_API_KEY` into the environment - VS Code does - and a worker
that inherits it authenticates as that key and bills the API account, quietly,
while the operator believes the run is spending a subscription allowance. A night
of workers is a large enough bill to matter. The runner removes it so the child
falls through to whatever `claude` itself is logged in as.

This also means the dollar figures in a run's output are **notional** on a
subscription: they are the CLI's own estimate of what the tokens would have cost
at API rates, useful for comparing steps and enforcing `budget_usd_per_step`, but
not an invoice. `docs/token-efficiency.md` measures where they go.

If you genuinely want a run to bill an API account, the runner will not let you
do it by inheritance - log `claude` in against that account instead.

The runner also **sets** `OVERNIGHT_STEP_ID`, `OVERNIGHT_STEP_KIND`,
`OVERNIGHT_REPO` (the tree the worker is to work in, its own worktree when the
step is isolated), `OVERNIGHT_MAIN_REPO`, `OVERNIGHT_OUT`, `OVERNIGHT_SPEC`, and
a `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` identifying this run - which is
what lets a later reset tell its own workers' commits from a third party's and
refuse to discard somebody else's work.

**The brief goes on stdin**, never argv. Windows caps a command line at 32,767
characters, so a long brief on argv truncates in production after passing every
small test; and `--tools` is variadic and swallows a following prompt argument.

**Every brief carries a tool-usage note**, regardless of kind: prefer Read, Grep,
Glob and an Explore-style subagent over Bash for reading and searching the
codebase, and reserve Bash for the test suite, build scripts and git. This is a
nudge, not a restriction - Bash stays fully available, because a build worker
must be able to run its gates and commit its own work, and a diagnostic must be
able to re-run the failing test it is asked to explain. A `cat`/`sed` read costs
far more input tokens than the equivalent structured call, which is the entire
reason to nudge it rather than the reason to ban it.

**Environment.** The worker inherits the runner's environment with these
changes:

- removed: `ANTHROPIC_API_KEY`, `CLAUDE_EFFORT`, `CLAUDE_CODE_SUBAGENT_MODEL`.
  With the API key present the CLI bills the API account rather than the
  subscription; an inherited effort setting silently overrides the plan's;
- set: `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`;
- set: `OVERNIGHT_STEP_ID`, `OVERNIGHT_STEP_KIND`, `OVERNIGHT_REPO`,
  `OVERNIGHT_OUT`, `OVERNIGHT_SPEC` - so a gate or a hook can know what is
  running;
- set: `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` - the run's identity
  (section 5).

**Never `--bare`.** It authenticates strictly through the API key and disables
CLAUDE.md discovery, so a worker would neither spend the subscription nor load the
project's rules.

**Output is `stream-json`**, so the log grows while the worker works and the
heartbeat can report its size. With `json` the log sits at brief-only size until
the worker exits, and a hang is indistinguishable from progress.

**CLAUDE.md is loaded** as it would be interactively. The project's rules apply
to every worker; put anything a worker must never do there, not only in the
preamble.

## 13. The self-test

    python selftest.py

Builds a throwaway repository with the standard layout, runs the runner against
`fake_worker.py` under a scripted scenario, and checks every path:

- preflight refuses a dirty tree;
- a build that passes first time; a review that returns `rework`; the rework that
  follows and passes;
- a build that fails twice - once by leaving no test, once by leaving the tree
  dirty - gets a diagnostic, and passes on the third attempt with the remediation
  ingested;
- a reflect that adds a step, which then runs and passes; a reflect that tampers
  with a completed step and is reverted, with the added step surviving;
- the discarded attempt is tagged for rescue and listed for the operator; worker
  commits carry the run's committer identity; the estimate is carried into the
  summary;
- resume: a relaunch starts no worker;
- a project with no git at all: warns once, skips `clean_tree` and review, says
  "cannot reset" plainly, carries the warning into the summary;
- a third party commits during a step and the gate then fails: the commit
  survives on the branch, the file is still on disk, the reset is refused loudly,
  the step halts, the run's exit code says so.

The fixture uses the production convention throughout - `review:s1`, the
`overnight/` layout, real pytest gates. It once used `review-s1` where every real
spec uses `review:s1`, and so missed a crash (`:` in a Windows directory name)
that killed a live run at its first review step after four passes. A fixture that
does not match production tests the runner against itself.

`OVERNIGHT_KEEP=1` keeps the scratch repository on failure.

Run it before every launch. A runner path that has not been exercised is a path
that will be exercised for the first time at 03:00.

## 14. Costs and calibration

Measured on the first two runs, opus/medium build steps, opus/high reviews:

| | minutes | turns | self-reported cost |
|---|---|---|---|
| a well-scoped build step | 7 - 13 | 40 - 80 | $2.40 - $3.50 |
| a build step carrying too much | 37 - 46 | 150 - 220 | $16 - $27 |
| a review | 10 - 20 | | about a third of the build it reviews |
| a rework after review | 10 - 13 | 60 - 80 | $5 - $5.50 |
| a night of 12 - 24 steps | 4.6 - 8 h | | $40 - $130 |

**The cost figure is the CLI's own estimate**, the API-equivalent price of the
tokens used. Workers run as `claude -p` with `ANTHROPIC_API_KEY` stripped from
their environment (section 12), so on a subscription they spend the
subscription's allowance and the dollar figure is notional - but
`budget_usd_per_step` is enforced against that notional figure, and will halt a
worker that exceeds it. Set it well above what a good step costs (45 was right
for opus/medium) or leave it unset.

**Where the money actually goes** is measured in `docs/token-efficiency.md`, over
every real worker this project has run: cost is API calls multiplied by the
context each carries, context re-reads are 56% of it, and a headless worker costs
the same per call as the same work done interactively. `python tools/tally.py
overnight/runs/<name>` re-measures any run, and reads an interactive transcript
too, so the comparison can be repeated rather than trusted.

**`expected_min` is the calibration loop.** Give every step an estimate. The
summary shows the actual beside it with the ratio, and marks anything past 1.5x.
An overrun is the symptom of a step carrying more than one deliverable; the next
plan can only be calibrated against numbers somebody kept. The hard `timeout_min`
belongs at roughly 2.5x the estimate: killing a worker at its expected time
destroys the work that was about to be committed, which is precisely the waste
being guarded against.

**Sonnet for mechanical steps.** Where the approach is single and obvious,
sonnet/medium is a third of the cost and as reliable. Where there is any breadth
of approach to weigh, opus.

## 15. Judgement calls

A step sometimes reaches a decision the brief did not make: a naming choice, a
default, a semantic reading of an ambiguous spec. The base behaviour is that the
worker writes the question to the decisions file with the interim it took, and
carries on - and the morning report leads with those decisions so a person can
ratify or reverse them. A step that cannot proceed without the answer stops and
is recorded as such; everything not depending on it proceeds.

<!-- VERIFY AGAINST THE IMPLEMENTATION before publishing: this section documents
     the expected-value triage as designed in docs/ROADMAP.md item 1. Confirm the
     config keys, the report format and the stopping rule match what was built. -->

The **expected-value triage** refines that. Rather than stopping on every
judgement, a classification pass asks whether this is a call the run should take
now or one that must wait. The rule is expected value, not category: proceed if

    P(wrong) x rework cost  <  time saved by not stopping

with three refinements that carry the weight:

- **Rework cost is measured over the dependency fanout**, not the step. A wrong
  call in the last step costs one step; the same call at step 3 of 28, with eight
  steps building on it, costs those eight plus reintegration. The same judgement
  at the same confidence is right to take late and wrong to take early. The runner
  knows the pending plan, so the fanout is computable.
- **Stacking has a budget.** Ten decisions at 90% confidence each leave 0.35 that
  the chain is sound. The run keeps a running confidence product; when it falls
  below about 0.5, it stops taking new judgement calls and banks what is built.
- **The override is detectability, not importance.** A wrong naming call is
  obvious in a diff. A wrong semantic call can produce output that passes every
  gate and reads correctly, and a review cannot catch what looks right. So every
  resolved decision leads the morning report with its confidence, the alternative
  rejected, and a one-line reason - making the error visible is cheaper than
  avoiding it.

Two consequences. Prefer the reversible implementation where there is a choice -
an isolated commit is a `git revert`, the same change woven through five files is
a re-run - because it lowers the rework term rather than estimating it. And a
stop is cheaper than it looks: a step that stops does not abort the run, it
skips, and only its dependent subtree waits.

The triage classifies and may inject a step; it never implements. An injected
step faces the normal gates and review like any other.

## 16. Limitations

- **`in-place` cannot tell your file from the worker's.** Isolation is the
  default: a build step runs in its own git worktree on a scratch branch and is
  integrated only once its gates pass, so your tree and branch are never written
  to, reset or cleaned, a commit from another window mid-step no longer HALTs the
  step, and a file you create mid-step is not the worker's problem. `in-place` is
  the opt-out, and this limitation is why it is the opt-out: sharing the tree,
  the `clean_tree` gate cannot distinguish work the worker failed to commit from
  a file somebody else created after the step began, so it fails the step for
  both - repeatedly, billably, and with the worker's good work discarded each
  time. Preflight tells you at the start whether your gates survive a fresh
  worktree; if they do not, `run.worktree_link:` is usually the answer and
  `in-place` is the fallback.
- **One worker at a time.** Steps are sequential by design; there is no
  parallelism and no dependency graph beyond plan order. A second runner on the
  same repository now refuses to start rather than fighting the first: the lock
  is `overnight/runs/.lock`, it holds the repository rather than the run, it is
  taken over automatically if the process that wrote it has died, and a report
  (`--list`, `--progress`, `--print-brief`) is not blocked by it. A lock written
  on another machine cannot be checked for liveness, so it is obeyed until
  somebody deletes it.
- **Progress is liveness, not percentage.** The heartbeat reports that a worker
  is alive and how much it has written, nothing finer (section 11).
- **Uncommitted edits to tracked files are unprotected** from a reset (section
  5). Commits and untracked files are protected; a modified tracked file is not.
- **A gate is only as honest as the command.** A test that cannot fail, a linter
  with no rules, `cmd: true` - the runner cannot tell. The review step exists
  because of this and is not optional for anything that matters.
- **The review is one model reading one commit.** It catches what a careful
  reader catches. It does not run the code beyond what it chooses to run, and it
  cannot catch a semantic error that looks right (section 15).
- **Windows-only, until somebody runs the self-test elsewhere.** Developed and
  run on Windows 11. The POSIX branches - process termination by `kill()` rather
  than `taskkill /T`, `fresh_shell` through a login shell, liveness by
  `os.kill(pid, 0)` rather than `tasklist`, a symlink rather than a junction on
  install - are written and have never been executed. Treat the runner as
  unverified on Linux and macOS rather than as portable: `python selftest.py`
  needs only `git`, `pytest` and a shell, and it will say.
- **The `claude` CLI is a moving target.** Flags (`--effort`, `--json-schema`,
  `--max-budget-usd`, `--disallowedTools`, `--permission-mode`) and the
  `stream-json` event shapes are what the CLI accepted when this was written. A
  CLI upgrade that renames one will show up as every worker exiting non-zero in
  the first minute; `--print-brief` and `--dry-run` do not catch it, the
  self-test's fake worker does not catch it, only a real `--only <one step>` does.
- **No daemon, no notification.** It is a foreground script that prints.
  Backgrounding it, surviving a reboot, or being told when it finishes are the
  operating system's job.

## 17. Troubleshooting

**"no git repository at or above the spec"** at launch, in a project that is a
repository - `--spec` was relative and the shell's working directory was not the
project. Give an absolute path.

**"REFUSING TO START: working tree is dirty"** - commit or stash. The runner will
not run over uncommitted work because its undo would destroy it.

**"REFUSING TO START: universal gate fails before any step"** - the plan's
`run.gates` do not pass on the tree as it stands. Fix the tree; a gate that fails
before any worker ran is a broken plan.

**"claude not found on PATH"** - the CLI is not installed or the shell that
launched the runner does not have it. A PATH entry added this session is not
visible to a terminal that was already open.

**Every worker exits non-zero within a minute** - almost always the CLI rejecting
a flag after an upgrade, or a login that has expired. Run one step with
`--only <id>` and read the top of its `attempt-1.log`.

**A step is HALTED** - somebody committed to the branch during the step and its
gate then failed. The tree was left alone. Read `discarded-commits.md` in the
step's directory, decide what to keep, and relaunch: HALTED is re-run on resume.

**A step PASSED with "gates pass but HEAD did not move"** - the gates were
satisfied by the tree as it was. Either the step was already done, or the worker
forgot to commit and the gates do not include `clean_tree` - in which case its
work is on disk, uncommitted, and the next reset will remove it. Add
`{clean_tree: true}` to the universal gates.

**The heartbeat's log size has stopped growing** - the worker is waiting on a
command that has not returned (a test suite, a build, a hung process) or has
stalled. `timeout_min` will kill it. If that is too far away, kill the process
tree yourself: the runner records the attempt as failed and moves on.

**A rescue is needed** - `git tag -l "rescue/*"` lists every commit a reset
discarded; `git cherry-pick <tag>` restores one. `discarded-commits.md` in the
step's directory says what each was.

**The self-test fails** - `OVERNIGHT_KEEP=1 python selftest.py` keeps the scratch
repository and prints its path, and the run.log tail is printed on failure.

## 18. Files in this repository

| file | what |
|---|---|
| `overnight.py` | the runner, one file; `python overnight.py --help` |
| `selftest.py` | every path, against the fake worker, in under a minute |
| `fake_worker.py` | a scripted stand-in for `claude -p`; the behaviours are listed at its top |
| `install.py` | link this checkout in as the skill; `--check`, `--force`, `--copy`, `--uninstall` |
| `SKILL.md` | what Claude Code reads for `/overnight`: a short router over the four modes |
| `references/planning.md` | stage 1 - read the project, interview, write the spec and briefs |
| `references/launching.md` | stage 2 - self-test, dry run, commit, launch, and the morning |
| `references/progress.md` | read a run in flight, or a finished one |
| `references/spec-format.md` | the steps file, gate forms, step kinds, resume |
| `examples/` | a complete fictional run package; `python examples/try_it.py` runs it end to end for no tokens |
| `docs/ROADMAP.md` | what is not built yet, and why each item matters |
| `CLAUDE.md` | the rules for working on this repository |
| `CHANGELOG.md` | what changed, release by release |
| `LICENSE` | GNU General Public License v3.0 |

## 19. Changelog

Recorded in `CHANGELOG.md`. The published release notes on GitHub summarise the
same changes for a reader who wants the highlights rather than the full list.

## 20. Licence

GNU General Public License, version 3 or later. The full text is in `LICENSE`.

In short: you may use, study, modify and redistribute this, including
commercially. If you distribute a modified version, you must release its source
under the same terms. There is no warranty.

    overnight - unattended runs of headless Claude Code workers
    Copyright (C) 2026 Paul Mason

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.
