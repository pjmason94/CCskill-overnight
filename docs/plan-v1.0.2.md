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
| 2 | stranded work is **retested**, not just reported | closes a defect that just cost a real decision | new self-test section | 60 min | OM |
| 3 | `--until` | roadmap 11; the largest piece | new self-test section - the clock has **none** today | 75 min | OM |
| 4 | `expected_min` is mandatory, and it schedules | roadmap 3; needs 3 | new self-test section | 60 min | OM |
| 5 | the plan seed | roadmap 7; docs only, so it lands last | nothing - and the commit says so | 45 min | OH |
| 6 | release notes and the tag | locks the version | `SELFTEST PASS` | 45 min | OH |

Pieces 1, 2 and 3 are independent of each other. 4 depends on 3. 5 and 6 are
last because they touch `SKILL.md` and `references/`, which are the files a new
session reads.

**All five open decisions were taken on 2026-09-07** and are written into the
sections below as settled. Two of them replaced the recommendation that was
offered, and both are better:

| | asked | decided |
|---|---|---|
| 1 | should stranded work change the verdict, or only the note? | **neither - retest it.** Run the step's gate against the stranded work; passing work merges as a successful re-run would, failing work fails |
| 2 | `--until` vs `--hours` precedence | `--until` wins |
| 3 | a step that will not fit: stop, or skip ahead? | **skip to a smaller one**, assuming no dependency - see the forward reference below |
| 4 | a step with no estimate: run it or refuse? | **it never reaches execution.** An unsized step is too complex and must fail the PLANNING cycle |
| 5 | the retry ladder (roadmap 13) in 1.0.2? | **no - deferred**, see below |

### Deferred, and what they are waiting for

**Roadmap 13, the retry ladder, is not in 1.0.2.** It is being reworked as part
of a larger piece on **step dependencies - what feeds into a step and what a step
feeds into** - and it waits on the A/B test reports covering FinKit's
`5b-report` specifically, which is the case the roadmap entry is argued from.
Nothing in this plan should pre-empt that design.

**That same dependency work is what makes piece 4's skipping safe.** Skipping a
step that will not fit and running a later one assumes the later one does not
depend on it, and today the plan carries nothing that could confirm that. Until
the dependency work lands, the assumption is unverifiable - so piece 4 makes it
loud rather than silent, and the section says how.

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

## 2. Work stranded on a scratch branch is retested, not reported

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

**The change - decided 2026-09-07. Do not report it, TEST it.**

Reporting stranded work and leaving the operator to decide was the weaker
answer, and it was rejected for the right reason: **the gate is already the
arbiter of whether work is good**, everywhere else in this runner. Work that
passes its gate should merge exactly as a successful re-run's work would; work
that fails should fail. Neither needs a person, and neither needs a new verdict.

So when a step is about to run and its scratch branch holds commits that are not
on the operator's branch:

1. put the stranded work in a worktree and **replay it onto the current base**;
2. if it will not replay, that is `NEEDS MERGE` - the verdict that already exists
   for exactly this, and the only case a person is needed;
3. if it replays, **run the step's gates against it**;
4. gates pass -> integrate and record `PASS`, with a note saying the work came
   from an earlier run and no worker was spent on it;
5. gates fail -> discard it exactly as today (tagged `rescue/...`, listed in
   `discarded-commits.md`) and run the step normally.

**The timing is the whole trick, and it is easy to get wrong.** This must happen
**before** `worktree add -B` moves the scratch branch, because that resets it to
base and the stranded commits become unreachable. The right home is
[`rescue_scratch_branch`](../overnight.py#L976), which is already the one place
that detects this exact condition - it just tags and resets today, and should
test first and only tag what fails.

**Why this is worth more than the reporting version.** In the b3-4 case it turns
a lost step into a free one: 11 minutes and $5.12 of work that already passed its
gates gets merged without spending a worker at all. Reporting it would still have
cost a full re-run.

**One thing to be careful of.** The stranded commit was built against an *older*
base. Replaying it onto the current base and then gating is what makes the test
honest - gating it on its own old branch would prove only that it used to work.
Step 1 is not optional.

**Proof.** A new self-test section, shown failing first: stranded work whose
gates pass is merged and the step records `PASS` with no worker spawned; stranded
work whose gates fail is tagged, discarded, and the step runs normally; stranded
work that will not replay is `NEEDS MERGE`.

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

**Decided 2026-09-07.**

1. **Precedence when both are given: `--until` wins**, wherever both appear, CLI
   over spec, and the banner says which was used and what it resolved to. Never
   silently pick one.
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

## 4. `expected_min` is mandatory, and it schedules

**Depends on 3** - a duration cannot answer "will this fit", a deadline can.

Today `expected_min` is read in exactly one place,
[`overnight.py:2405`](../overnight.py#L2405), for the estimate-vs-actual ratio in
`SUMMARY.md`. Nothing consumes it for scheduling. The roadmap's target line is
`not starting b5-2 (est 18 min, 14 min left)`.

### 4a. An unsized step fails PLANNING, not execution

**Decided 2026-09-07, and it replaced the recommendation offered.** The proposal
was to run an unsized step anyway and log that it could not be checked. The
decision is stronger and better placed: *a step that cannot be given a time
estimate is too complex, and that is a planning failure, not an execution one.*

The evidence agrees. Across FinKit's 36-step ledger, every build step carrying an
estimate finished in 8-24 minutes; every step carrying none ran 28, 33, 52, 53,
54, 119 and 151. Writing a number does not make work faster - a step nobody sized
is a step nobody scoped. Letting one through to execution just moves the failure
somewhere it cannot be fixed.

**So `expected_min` becomes required on any build step still to run**, validated
when the spec loads, refusing with the offending step ids named.

**Checked before deciding, because a hard refusal could have stranded live
plans:**

| project | build steps | unsized | unsized **and still to run** |
|---|---|---|---|
| FinKit | 30 | 13 | **0** |
| Woodwork Guru | 32 | 0 | **0** |

All thirteen of FinKit's unsized steps have already run and recorded `PASS`.
Validating **only steps still to run** therefore costs nothing today, and both
plans load unchanged - no rebuild needed. It is also the principled line: a
completed step's estimate is moot because its *actual* is recorded, and rewriting
history to satisfy a new rule teaches nobody anything.

The planner must also stop emitting unsized steps - `references/planning.md`
already argues for it, and this makes it enforceable rather than advisory.

### 4b. A step that will not fit is skipped, not the end of the run

**Decided 2026-09-07, and it replaced the recommendation offered.** Stopping the
run was proposed on the grounds that skipping could build on ground never laid.
The decision is to **skip to a smaller step that fits, assuming no dependency**,
because stopping wastes a night's remaining time over a hazard that is about to
be addressed properly.

**The assumption must be loud, because nothing can currently verify it.** The
plan carries no record of what feeds a step or what a step feeds - that is coming
as a separate piece of rework (see *Deferred* above), and it is what will make
this safe rather than merely reasonable. Until then:

- the skipped step records **`NOT RUN`** - it already means "no worker read the
  code" ([`overnight.py:126`](../overnight.py#L126)), it resumes cleanly and it
  wakes nobody. The note must name both figures and distinguish it from a
  usage-wall casualty, which is the other thing producing `NOT RUN`;
- the log and `SUMMARY.md` must say **the plan order was departed from** - which
  step was skipped and which ran in its place. A reordering nobody can see is the
  failure mode here, and making it visible is the cheap half of the fix.

**Proof.** A new self-test section: a step whose estimate exceeds the remaining
time is `NOT RUN` with a note naming both figures; a later, smaller step still
runs; and the summary says the order changed.

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
| 13 - the retry ladder | deferred 2026-09-07. Being reworked inside the larger **step dependency** piece, and waiting on the A/B test reports for FinKit `5b-report` - the case it is argued from |
| step dependencies - what feeds a step, what a step feeds | a separate piece of rework, not yet written up. It is what makes 4b's skipping provably safe rather than merely reasonable |
| 1 - the expected-value triage | the decision hook has to exist first; there is no structured way for a worker to raise a judgement today |
| 4 - cross-platform verification | needs a Linux or macOS box. Nothing on this machine can do it |
| 5 - packaging | by its own entry, it does not matter until somebody wants the skill without cloning |
| 6 - a worked example with a real morning | a by-product of a run, not a build step |
| 2 - isolation proven against a real worker | earned by running this repo overnight on a quiet night, once 3 and 4 give it something gateable to do |
