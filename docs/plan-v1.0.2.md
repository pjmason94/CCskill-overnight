# Plan - the rest of 1.0.2

Written 2026-09-07. What is left before `v1.0.2` is tagged, in the order it
should be done, with the decisions that need a person marked as such.

**This is not a second roadmap.** `ROADMAP.md` says what each item is and why it
matters, and it stays the place that argument lives. This file carries only what
a roadmap cannot: the sequence, the code the change actually lands in, what
proves each piece, and the open questions. Where the two meet, the roadmap wins
on *what* and this file wins on *when*.

**It is also not a steps spec.** This work is executed at the keyboard, not by an
unattended run. That was decided on the evidence below, not by default.

---

## The constraint that governs the whole sequence

This checkout **is** the live skill. `install.py` links it to
`~/.claude/skills/overnight/`, so every project on the machine loads these files,
and an edit here is live for the next thing that reads them.

FinKit and Woodwork Guru have real overnight runs planned. That gives one hard
rule for every commit below:

> **Nothing in this plan is committed while a run could be launched or relaunched
> from this checkout.**

The three cases are not the same, and only the first is safe:

| | effect of a commit here |
|---|---|
| a run **already** in flight | **safe.** Python loads `overnight.py` once at process start; the run keeps the code it started with, to the end - including across a park, which is the same process |
| a run **launched or relaunched** afterwards | **not safe.** It reads whatever is on disk. A 3am relaunch after a crash picks up half-finished work |
| a **new interactive session** | **not safe.** `SKILL.md` and `references/` are read at session start, so a session opened mid-night loads a partly-rewritten procedure |

So the window for this work is when no run is live and none is about to be -
after the morning reports are read, not before the night starts.

It is also why this is not an overnight run of its own: a run of this repo would
be committing changes to the runner that other runs launch from, on the same
night those runs are launching.

---

## Why this is interactive work and not a run

Recorded because the question was asked twice and deserves an answer that
survives the session.

The gate cost is real but was **not** the deciding argument. This project's
whole test suite is one command, `python selftest.py`, with no node ids and no
way to run a section alone - `main()` takes no arguments and its 23 sections are
`print()` statements inside one linear function ([`selftest.py`](../selftest.py)).
So every gate is the whole suite, and every attempt pays it. At 12 min 42 s that
is roughly an hour of gates across a five-step night, which an eight-hour run can
afford. Piece 0 removes even that, and is what would make a future run of this
repo cheap to gate - but it does not change the two arguments below, which are
the ones that decided it.

The deciding arguments were:

1. **Two of the six pieces cannot be gated at all.** The plan seed and the
   release notes are prose in `SKILL.md`, `references/` and `docs/releases/`.
   No command exits 0 or 1 on whether a procedure reads well. A step whose only
   judge is a review worker is the weakest thing the runner does.
2. **The live-skill constraint above**, which an unattended worker cannot honour
   because it does not know what else is running.
3. **No calibration.** This project has never had a run, so there is no ledger
   and no actuals - every estimate below is from first principles, which is the
   case the planning procedure explicitly warns is weakest.

The counter-argument is real and is not dismissed: roadmap item 2 says worktree
isolation "is still unproven against a real worker", and item 6 wants a real
morning to teach from. A run on this repo would produce both as a by-product.
That is a good reason to run this repo overnight **later** - on a night when
nothing else is launching, and against work that is gateable.

---

## The order

| # | piece | why here | proves it | est | tier |
|---|---|---|---|---|---|
| 0 | run the self-test by section | every piece after it is iterated on 13 minutes at a time otherwise | its own check-count invariant | 90 min | OM |
| 1 | the published self-test runtime is wrong | free, and a wrong number outranks a bug | reading the clock | 15 min | SM |
| 2 | a blocking step must declare stranded work | closes a defect that just cost a real decision | new self-test section | 45 min | OM |
| 2a | the retry ladder | roadmap 13; near zero cost, no extra worker | new self-test section | 45 min | OM |
| 3 | `--until` | roadmap 11; the largest piece | new self-test section - the clock has **none** today | 75 min | OM |
| 4 | `expected_min` refuses a step it cannot finish | roadmap 3; needs 3 | new self-test section | 45 min | OM |
| 5 | the plan seed | roadmap 7; docs only, so it lands last | nothing - and the commit says so | 45 min | OH |
| 6 | release notes and the tag | locks the version | `SELFTEST PASS` | 45 min | OH |

Pieces 1, 2, 2a and 3 are independent of each other. 4 depends on 3. 5 and 6 are
last because they touch `SKILL.md` and `references/`, which are the files a new
session reads.

**The working rule these serve** (Paul, 2026-09-07): *the self-test sections run
during a piece of work are the ones relevant to it; the FULL suite runs before a
commit and push.* At 12 min 42 s a full suite per iteration is not a test loop,
it is a coffee break, and the honest consequence of paying it every time is that
it gets skipped. Piece 0 exists to make the first half of that rule possible.

---

## 0. Run the self-test by section

**Prerequisite for the working rule above, and cheaper than it looks.**

Today `main()` takes no arguments and its 23 sections are `print()` statements
inside one linear function, so the only thing that can be run is all of it.

**But the sections are far more separable than that suggests.** Mapping every
`make_repo` call against every section boundary:

| sections | state |
|---|---|
| 1, 2, 2b, 3 | share one repository built at [`selftest.py:358`](../selftest.py#L358) and run in sequence - 1 dirties the tree, 2 runs the full scenario, 2b reads the ledger it wrote, 3 resumes it. **One indivisible block** |
| 4, 5 | build their own (`make_plain_dir`, `make_repo`) |
| 6, 7 | read a repository an earlier section built - needs checking before either can be selected |
| **8 through 22** | **each builds its own repository at its own start. Fifteen of the twenty-three are already independently runnable** |

And the sections this plan adds - pieces 2, 2a, 3 and 4 - are new ones, which are
self-contained by construction. So `--only 23` works for exactly the case the
work needs, from the first day.

**Shape.** `--only 13,17` and `--from 17`; a bare invocation still runs
everything and still prints `SELFTEST PASS`. The 1-2-2b-3 block is selected as a
block, and asking for a section that cannot stand alone says so rather than
running a subtly different test.

**The one real hazard, and its guard.** The change is a mechanical
re-indentation of 1,150 lines, and its failure mode is silent: a section
accidentally dropped from the run still prints `SELFTEST PASS`, and the suite
gets quietly weaker. So the same piece adds a **total check-count invariant** -
the full suite counts its own checks and fails if the number is not the expected
one. It is 235 today. That turns the silent failure into a loud one, and is worth
having on its own terms.

Do this first. Everything after it is otherwise iterated on in 13-minute
increments.

---

## 1. The published self-test runtime is wrong

**No decisions. Do it first.**

`selftest.py` is documented as running "in under a minute". Measured on
2026-09-07 on this machine it takes **12 minutes 42 seconds** - 17:42:08 to
17:54:50, 235 checks, 0 failures. That is **thirteen times** the published
figure. A previous session already noticed this
(`Session history/2026-09-07_1132`) and it was never corrected, which is how a
wrong number survives: nobody goes looking for it.

Sites, all the same claim:

- [`README.md:125`](../README.md#L125) and [`README.md:1215`](../README.md#L1215)
- [`SKILL.md:110`](../SKILL.md#L110)
- [`selftest.py:16`](../selftest.py#L16) - the module docstring
- `CLAUDE.md:37` - the project file, and `~/.claude/CLAUDE.md` if it repeats it

**Do not touch `CHANGELOG.md:341`.** It says "about two minutes" inside the
released `[1.0.0]` section, and a released section is never edited. The
correction belongs in `[1.0.2]` as a `Fixed` entry that says the old figure was
wrong and what it actually is.

Give the honest shape, not a single number: it is dominated by fixtures that wait
on real clocks - the stall watchdog and the usage-wall probe - so it is minutes,
not seconds, and it grows as those are added to.

---

## 2. A blocking step must declare work stranded on its scratch branch

**The evidence.** Woodwork Guru, 2026-09-07. `b3-4-nested-drawer`'s worker
finished at 02:32:39 - exit 0, 11.3 min, $5.12, committed `4b2156e`, tree clean -
and the run died before integrating it. The relaunch could not create the
worktree and recorded:

    outcome: "STUCK"   attempts: 0
    note: "could not create the worktree for this step"

which reads as *nothing happened*. In fact a complete, gates-passing step sat on
`overnight/engine-b0-b8/b3-4-nested-drawer`, and still does. The morning had no
way to know, and the natural next move - re-run the step - rebuilds work that
already exists.

**What already exists, and why neither covers it.**

- `NEEDS MERGE` ([`overnight.py:2005`](../overnight.py#L2005)) is set in exactly
  one place: gates **passed** in isolation and the replay **conflicted**. It is
  blocking and deliberately not re-run. b3-4 never reached integration.
- `rescue_scratch_branch` ([`overnight.py:976`](../overnight.py#L976)) does tag
  the stranded commit - but only when the step is **re-run**, because that is
  when `worktree add -B` would move the branch. That is after the morning has
  already decided what to do, which is the one moment the information was worth
  having.

**The change.** At the point a blocking outcome is recorded, ask whether
`scratch_branch(step_id)` exists and holds commits that are not on the
operator's branch - `commits_since(base, tip=branch)` already answers it - and if
so name them in the ledger note and in `SUMMARY.md`, with the sha and the count.

**Decision needed.** Does stranded work change the *outcome*, or only the *note*?

> **Recommendation: the note and the summary only.** Making it a new blocking
> outcome, or reusing `NEEDS MERGE`, would change resume semantics: `NEEDS MERGE`
> is deliberately excluded from `RERUN_OUTCOMES` because re-running would do the
> work twice. For b3-4 re-running is *correct* - the commit was never gated
> against this run's base - so the operator needs the *fact*, not a different
> resume rule. Changing the outcome would take the choice away from them.

**Proof.** A new self-test section: a step blocked with commits left on its
scratch branch names them; a step blocked with a clean branch says nothing extra.
Show it failing against the current runner first.

---

## 2a. The retry ladder - roadmap 13

Added to the roadmap after this plan was first written; folded in because it is
cheap, it needs no extra worker, and the evidence for it is already measured.

Attempt 2 is handed the same brief as attempt 1, byte for byte, and told nothing
about why attempt 1 failed - the roadmap has the log lines showing identical
character counts across attempts. The diagnostic worker runs only after the
**second** failure, so the first retry is a pure re-roll.

**The runner already holds every fact needed.** It knows which gate failed, it
has the gate's output, and it has quarantined whatever untracked files the
attempt left behind. None of that needs a model. The ladder:

| attempt | given |
|---|---|
| 1 | the brief |
| 2 | the brief + the failed gate's **name** and **output**, truncated, stated as fact |
| 3 | the brief + `remediation.md` from the diagnostic worker, as now |

**Decision needed.** Nothing structural, but one judgement worth stating rather
than discovering: attempt 2's addition is deliberately **not** the predecessor's
code, which would anchor the retry on a design that has already failed once. The
considered read of what went wrong stays at attempt 3, where a worker is paid for
it.

**Where it lands.** `build_brief(step, attempt, remediation, rework)` already
takes the attempt number, so the hook exists; the failed gate's name and output
have to be carried from the gate runner into the next attempt's brief.

**Proof.** A new self-test section: attempt 2's brief contains the failing gate's
name and output; attempt 3's contains the remediation, as now. Both are already
assertable - `run_worker` writes the full brief into every worker log.

---

## 3. `--until`

Roadmap 11 has the full argument and the shape is settled there. What it does not
have is where it lands.

**Where the clock lives today:**

- [`overnight.py:696-699`](../overnight.py#L696-L699) - `hours` from `--hours` or
  `run.hours` (default 6), then `stop_at = time.time() + hours * 3600`
- [`overnight.py:1635`](../overnight.py#L1635) and
  [`overnight.py:2646`](../overnight.py#L2646) - the only two places `stop_at` is
  tested
- [`overnight.py:1631`](../overnight.py#L1631) - already formats `stop_at` as
  `%H:%M` in the park message, so an absolute time is not a new idea
- [`overnight.py:2620`](../overnight.py#L2620) - the run banner, "stop starting
  after {hours} h"
- [`overnight.py:2744`](../overnight.py#L2744) - the spec reference `--format`
  prints
- [`overnight.py:3037`](../overnight.py#L3037) - argparse
- `README.md` lines 222, 374, 528, 626 - the clock, the parking rule, the spec
  key table and the flag table

**Decisions needed.**

1. **Precedence when both are given.** Recommendation: `--until` wins over
   `--hours` wherever both appear, CLI over spec, and the banner says which was
   used and what it resolved to. Never silently pick one.
2. **Resolution.** `07:30` means the next occurrence of 07:30. Launched at 07:00
   that is 30 minutes, not 24.5 hours. Log it **once, as an absolute datetime**,
   so the log is never ambiguous about which 07:30 was meant.
3. **`--hours` stays.** It is a running contract; deprecated in the README, not
   removed, computed against the start as it is now.

**Proof, and a gap this exposes.** There is **no self-test anywhere that the
clock stops a run**. Fixtures set `hours: 1` in seven places and nothing asserts
on it. So this piece writes the first clock test, and that is worth doing on its
own terms: a deadline already passed must start no step and must say so.

---

## 4. `expected_min` refuses a step it cannot finish

**Depends on 3** - a duration cannot answer "will this fit", a deadline can.

Today `expected_min` is read in exactly one place,
[`overnight.py:2405`](../overnight.py#L2405), for the estimate-vs-actual ratio in
`SUMMARY.md`. Nothing consumes it for scheduling. The roadmap's target line is
`not starting b5-2 (est 18 min, 14 min left)`.

**Three decisions, and the second is the one that matters.**

1. **The outcome.** Recommendation: reuse **`NOT RUN`**. It already means "no
   worker read the code" ([`overnight.py:126`](../overnight.py#L126)), it is in
   `RERUN_OUTCOMES` and out of `BLOCKING_OUTCOMES`, which is exactly right - a
   resume picks it up and nobody is woken. The note must distinguish it from a
   usage-wall casualty, which is the other thing that produces `NOT RUN`.
2. **Stop, or skip to something that fits?** Skipping ahead reorders the plan,
   and later steps usually depend on earlier ones, so a "helpful" skip can build
   on ground that was never laid. Recommendation: **stop starting entirely**,
   matching the deadline's existing behaviour. The alternative - honour the
   spec's own ordering but let an independent later step run - is defensible and
   is what roadmap item 1 would eventually want, but it needs the dependency
   information the plan does not carry today.
3. **A step with no `expected_min`.** Recommendation: **start it**, and say in
   the log that it was unsized so the refusal could not be evaluated. Refusing an
   unsized step would silently drop work over a missing field. The planning
   procedure already argues every build step should carry one; this makes the
   absence visible rather than fatal.

**Proof.** A new self-test section: a step whose estimate exceeds the remaining
time is `NOT RUN` with a note naming both figures, and one that fits still runs.

---

## 5. The plan seed - roadmap 7

Docs only: an argument through `SKILL.md`, a section in
`references/planning.md`. No runner change, so nothing here can affect a live
run - but it does change the live skill for the next session, which is why it is
second to last.

The roadmap's one load-bearing rule: the planner may merge, split, reorder or
drop what it reads, **but it must list what it dropped and why, at the top of the
proposal.** A seed that is silently filtered is worse than no seed, because the
user believes their document is the plan.

No self-test covers it, and the commit message should say so rather than leave it
silent - the project rule is "covered by the self-test, or says why not".

---

## 6. Release notes, then the tag

`docs/releases/v1.0.2.md`, written **before** the tag and published as the GitHub
release body. Per the project rules, in this order:

1. **The headline change and the reasoning.** For 1.0.2 that is the continuation
   model: one attempt may now take several workers, so the same money buys
   progress instead of repetition.
2. **What is now different for an existing plan.** `OVER BUDGET` is a new
   blocking outcome, so a plan that used to march on now stops. `run.stall_min`
   defaults to 10 minutes and kills a silent worker that previously ran to
   `worker_timeout_min`. Per-step `budget_usd` overrides the run-level cap.
3. **Anything a user may have quoted that was wrong.** The self-test runtime
   (piece 1). This is the section that exists for exactly this case.
4. **The upgrade steps.**

Cut only from a checkout whose `selftest.py` passes, and only after the
`[1.0.2]` heading carries a real date instead of `unreleased`. The section locks
at the tag and is never edited again.

---

## Not in 1.0.2

| roadmap item | why not |
|---|---|
| 1 - the expected-value triage | the decision hook has to exist first; there is no structured way for a worker to raise a judgement today |
| 4 - cross-platform verification | needs a Linux or macOS box. Nothing on this machine can do it |
| 5 - packaging | by its own entry, it does not matter until somebody wants the skill without cloning |
| 6 - a worked example with a real morning | a by-product of a run, not a build step |
| 2 - isolation proven against a real worker | earned by running this repo overnight on a quiet night, once 3 and 4 give it something gateable to do |
