# Audit findings - the implementation against the theory

Written 2026-09-08, executing `plan-architecture-audit.md` at FH. Axes A to E
were run as six Sonnet subagents in one parallel batch, each with an explicit
file list and a pipe-delimited output contract capped at 25 lines; they returned
70 raw lines in total (A1 5, A2 5, B 10, C 13, D 18, E 19). Axes F and G were
done in the main context with the findings in hand.

**Every severity-1 line was verified by reading the code before it appears
here.** Lines marked *agent* are severity 2 or 3 and are carried as reported.
Where a subagent's severity did not survive verification the change is stated.
Nothing in this file has been fixed. The proposed fix set is at the end.

Severity: **1** = a live run behaves wrongly or wastes real time; **2** = a
procedure misleads the planner or a user; **3** = cosmetic or stale wording.

The three defects already fixed in `[1.0.3]` (the mandated full-suite gate, the
missing "feeds" half of a brief, the opus build default) were excluded from the
hunt. Two residues of the first turned up anyway and are listed as F7.

---

## Severity 1 - verified

### F1. A brief or preamble path that does not exist is read as an empty string

**Claim.** `README.md:635` - `brief` is a path relative to the repository root,
required on a build step. `README.md:619` - `run.preamble` is prepended to every
build brief.

**What happens.** `load_spec` (`overnight.py:453`) checks only that the key is
present. `preflight` (2822) never stats it. `build_brief` (2084) reads it
through `read_text` (362-366), which returns `""` on any `OSError`. So a typo in
a brief path spawns a worker at 02:00 whose instructions are the preamble, a
header, the tool-usage note and a gate list - three attempts and an opus
diagnostic over a step nobody wrote. The same for `run.preamble`: a wrong path
silently drops the project conventions from every brief of the night.

The inconsistency is the tell: a plan rewritten by a **reflect** step *is*
checked for this (2649-2650 refuses "a brief that does not exist"), so the
initial plan gets less validation than a mid-run rewrite of it.

**Fix.** Stat every build step's brief that is still to run, and `run.preamble`
if set, at preflight (the runner knows the repository root there; `load_spec`
does not). Refuse with the full list, exit 2, before the lock is taken.
Self-test: a spec naming a missing brief exits 2 with no worker log written.
Found during verification of C-1/C-7 (the agents reported the missing check;
reading `read_text` showed the consequence).

### F2. A mistyped `effort`, `model` or budget on one step parks the run for the night

**Claim.** `README.md:684` - effort is `low`, `medium` or `high`. `README.md:641`
- a bad `budget_usd` is refused at load, not at 2am. Implicit everywhere: the
circuit breaker exists for *environmental* failure.

**What happens, verified in the runner.** `load_spec` validates a step's
`budget_usd` (492-498) but not `run.budget_usd_per_step`, and validates neither
`effort` nor `model` at all. `worker_argv` (1737, 1760-1763) passes all three to
the CLI as typed. A value the CLI rejects gives a worker that exits non-zero
with no result event, which is exactly the shape `count_barren` (1866-1874)
calls barren. Three attempts, three barren, the wall is called, the step is
recorded `NOT RUN`, the run parks (3011). The probe (1887) is haiku with **no
effort and no budget flag**, so it answers `ALIVE`; the step is put back in the
queue (3022) and `choose` (902) picks it again because it is first and it fits;
three more barren, park again. Every `park_poll_min` until `stop_at`.

Verified in code: the loop. Assumed, not verified: that the CLI rejects a bad
`--effort` or non-numeric `--max-budget-usd` before making an API call (it is an
argument parser; almost certainly), and how it fails on an unknown `--model`
(unverified - it may return an error result event, which would be caught as
`did_something` and not be barren at all).

The same loop fires with a purely environmental cause: **one model withdrawn**
while haiku still answers. The probe says alive, the step says nothing, all
night.

**Fix, two layers, both deterministic.** (a) At load: `effort` in the set the
runner itself documents, `run.budget_usd_per_step` through the same positive-
number check the step key already gets. `model` cannot be enumerated without
drifting stale (the C agent was right about that) - which is why (b) is the one
that matters: in the wall path, **the probe answering while the same step goes
barren a second time means the step, not the environment.** Record it as a
blocking outcome with the worker's argv and stderr tail in the note, and move to
the next step instead of parking again. Python comparing two facts it already
holds; no model in the loop. Self-test: a fake-worker scenario that exits 2
immediately with no result event on one step and behaves on the next; assert the
run parks at most once, records the step as blocking, and runs the next step.

### F3. `budget_usd` on a review, reflect or diagnostic is silently ignored

**Claim.** `README.md:641` - `budget_usd` applies to "any [step] with a worker"
and overrides `run.budget_usd_per_step`.

**What happens.** `worker_argv` (1760-1761) falls back to the run-level cap when
no `budget=` is passed. Only the build path passes one (2383, `budget=cap`).
`run_review` (2539), `run_reflect` (2620) and `run_diagnostic` (2507) pass
nothing, so the run cap applies to them and the step's own does not. A planner
who gives a reflect step a larger cap than the builds, as the README invites,
gets the build cap and an `INCONCLUSIVE` or a `NO CHANGE` when it trips.

**Fix.** Pass `budget=self.budget_for(step)` at the three call sites. Self-test:
a review step carrying `budget_usd` asserts `--max-budget-usd <its value>` on the
argv line of `review.log` (section 1 already reads that line at `selftest.py:494`).

### F4. A `kind: gate` step never runs the universal gates

**Claim.** `README.md:642` - `gates` on a build or gate step are "this step's own
gates, run before the universal ones".

**What happens.** `run_gate_step` (2698) runs `step.get("gates")` alone.
`all_gates` (1714) exists and every build step uses it. Since `1.0.3` the
planner is told to put the full suite in a `kind: gate` checkpoint; a project
whose lint or hygiene check lives in `run.gates` gets none of it at the
checkpoint, and the README says it does.

**Fix.** Either `self.all_gates(step)` in `run_gate_step`, or amend the README.
Recommend the code: the README is what the planner read. Self-test: none exists
for `kind: gate` at all (T3 below); one fixture covers both.

### F5. `REVIEW REWORK FAILED` is neither resumed nor blocking, and the documented outcome never exists

**Claim.** `README.md:763-764` and `references/spec-format.md:178` - a step
recorded `REWORK FAILED` did not complete and is re-run on relaunch.

**What happens.** `RERUN_OUTCOMES` (117) holds `"REWORK FAILED"`. The value the
ledger actually receives is `"REVIEW REWORK FAILED"` (2599), on the review step.
The bare form is produced by `run_attempts` (2489) and returned to `run_review`,
which records nothing under it - `run_build` does not call `record`, and the
main loop records only the review's own entry (3032). So: the `RERUN_OUTCOMES`
entry is dead; the review is **complete** on resume; it is not in
`BLOCKING_OUTCOMES` (3216); `--mode` says `RUN` or `REPLACE?` over a commit that
a reviewer found wanting and a rework failed to repair. The exit code (3068) is
the only signal, and nothing reads it at 07:00.

**Fix - a design choice, Paul's.** (a) Make it blocking: a person decides
whether the unfixed findings matter before anything builds on that commit. This
is the answer axis G gives (a judgement that failed twice is handed to a person,
not to a third model). (b) Make it resumable, as documented: the review re-runs
against the same sha, likely returns the same verdict, and spends another opus
rework. Recommend (a), and correct both documents to say so. Self-test: none
exists for this path (T5).

### F6. `NEEDS MERGE` is blocking in the runner and absent from the skill's BLOCKED procedure

**Claim.** `SKILL.md:28` and `36-39` - BLOCKED means STUCK, HALTED or OVER
BUDGET, "none of the three". `references/spec-format.md:184` - the same three.
`references/blocked.md` - zero occurrences of the word.

**What happens.** `BLOCKING_OUTCOMES` (3216) has four members and `README.md:777`
lists all four. `NEEDS MERGE` is the one that means *work exists on a scratch
branch that the tree does not have* (3035-3043 stops the run for it). The skill
reports BLOCKED, loads `blocked.md`, and has no procedure for the one outcome
where the user has to do something with git before relaunching.

**Fix.** Docs only. A `NEEDS MERGE` section in `blocked.md` (where the branch is,
how to inspect it, how to land it, then relaunch), and the fourth cause in
`SKILL.md` and `spec-format.md`. Verified: `grep MERGE references/blocked.md`
returns nothing.

### F7. Two residues of the full-suite universal gate

`references/spec-format.md:45-46` and `examples/steps.example.yaml:36-37` both
show `{name: full suite, cmd: python -m pytest -q}` under `run.gates`, annotated
"universal; appended to EVERY build step". `planning.md` and the README were
corrected in `1.0.3`; the reference the planner opens for the key format and the
example it is told to copy the shape from were not. The runner's own embedded
reference (`SPEC_FORMAT`, 3144) is clean.

**Fix.** Cheap universal gates in both (lint, `clean_tree`), and a `kind: gate`
checkpoint step in the example carrying the suite.

---

## Severity 1 - documented behaviour with no self-test (axes D and E)

Verified untested by grep of `selftest.py` for the identifier, outcome string or
spec key. Ranked by what breaks if the behaviour silently regressed.

| | behaviour | evidence | blast radius if it broke |
|---|---|---|---|
| T1 | `STRIP_ENV` removes `ANTHROPIC_API_KEY`, `CLAUDE_EFFORT`, `CLAUDE_CODE_SUBAGENT_MODEL` from every child (`README.md:1023`) | no occurrence in selftest.py; `fake_worker.py` reads `os.environ` but never asserts on these | a night billed to the API account instead of the subscription; effort inherited at +40% |
| T2 | `on_fail: record` and `on_fail: revert` (2562-2577) | only `on_fail: rework` appears in any fixture | a wrong revert resets the operator's branch; `record` spawning a rework spends opus it was told not to |
| T3 | `kind: gate` steps (2694) | no fixture has one | a checkpoint that reports PASS without running, or FAIL that stops nothing |
| T4 | `cmd_empty` and `fresh_shell` gate forms (1664, 1682) | neither string in selftest.py | a gate that passes on output it should fail on |
| T5 | `REFLECT NO CHANGE` (2661), `REVIEW REWORK FAILED` (2599), bare `REWORK FAILED` (2489) | none of the three strings | see F5 and G-4 |
| T6 | `--rerun` re-runs passed steps (`README.md:755`) | no occurrence | a flag that silently does nothing on the morning the operator wants it |

`file:` gates, reported untested by E, are exercised once as a fixture
(`selftest.py:1052` replaces a cmd gate with `file: local-only.txt` so a
worktree probe fails) - proxy coverage, downgraded to 2.

The T1 fix is one line in `fake_worker.py` (exit 99 if any `STRIP_ENV` name is
present) and one check. T2-T6 are fixtures; T3 and F4 share one, T5 and F5
share one.

---

## Severity 2

Where a line is not marked *agent* it was checked.

- **The `SKIPPED` resume rule is wrong in the README, right in the code and in
  spec-format.** `README.md:761-762` says a step "SKIPPED for a reason that will
  not change" is skipped on resume; `RERUN_OUTCOMES` holds bare `SKIPPED`, so
  every skipped step is re-run. Harmless - a review skipped because its subject
  had not passed re-skips without spawning a worker, and runs if the subject has
  since passed, which is what is wanted. Downgraded from the B agent's 1: fix the
  README wording.
- **Running step spans the stop time; a review is never declined for the clock.**
  `README.md:243, 279`. Section 24 tests neither: the stop is checked at the top
  of the loop only (2968), so the structure makes interruption impossible, but
  nothing asserts it; section 25 reaches a review after an undersized build only
  to SKIP it (`selftest.py:2096`), so "a review runs when the clock is tight" is
  proxy coverage. Downgraded from D's 1 on the structural argument.
- **`OVER BUDGET` exit code** is documented as 1 (`README.md:787`) and section 21
  never asserts `returncode` (1543-1586). Proxy only.
- **`timeout_min` at ~2.5x `expected_min`** (`README.md:640`): a load-time
  *warning* when `timeout_min <= expected_min` is cheap and deterministic; a
  refusal would be too strong (C-3).
- **A gate's test node id named in the brief** (`README.md:657`) and **a `## Feeds`
  heading in every brief** (`planning.md:237-245`, which fixes the heading):
  both checkable at preflight as *warnings* - substring absent from the brief
  text. Advisory today (C-5, C-6).
- **A reflect every three to five builds** (`planning.md:124`): stays advisory
  (C-8). **"If the description needs the word 'and'"** (`planning.md:119`): stays
  advisory, the heuristic is unsafe (C-9). **`model` values**: stays advisory,
  the CLI's set drifts (C-10) - and F2(b) is what catches a wrong one.
- **The `run:` key table is not exhaustive** (`README.md:602-628`): `isolation`,
  `worktree_link`, `worktree_root` are read at 781-788 and 1128-1131 and absent
  from the table, while `spec-format.md:4` calls the table exhaustive (A1, B-5).
- **The runner's `run.log` heartbeat is liveness-only** (size of the worker log
  each minute, `README.md:970`) while the user-level rule now asks every long run
  for percent and ETA. The run *has* a denominator (steps, `expected_min`) and
  `progress.py` / `SUMMARY.md` carry it; state the exception in the README (B-6,
  *agent*).
- *agent*: quarantine `MANIFEST.txt` contents not asserted (`selftest.py:448`);
  `done:` fields asserted by key presence not value (599); commit author left to
  git config untested (`README.md:469`); no-git reflect "uncommitted" path
  untested (498); `--continuations` / `--wall-threshold` CLI overrides untested
  (748); full-scenario exit asserted as `in (0, 1)` at 566.
- *agent*: `--format` untested; `install.py` (all of it, `--copy` included) has
  no test; `review_timeout_min` / `reflect_timeout_min` / `diagnostic_timeout_min`
  untested; `run.worktree_root` untested.

## Severity 3

- `overnight.py:3125` `SPEC_FORMAT` comment still says the build default is `OM`;
  it is `SM` (A2).
- `README.md:707` brief order omits `TOOL_USAGE_NOTE` between header and brief
  (A1).
- Dead parameters: `disallow` in `worker_argv` (1736) is never passed;
  `remove_worktree(keep_branch=False)` (1330) is never called (E).
- `--run <name>` (3371) narrows `--progress` and is not in the README flag
  table; `--repo` and the `--continuations` flag itself untested; `read_text` /
  `compact_transcript` truncation untested (E, *agent*).
- `README.md:152` layout diagram omits `briefs/_reflect.md`; `README.md:225`
  morning reading order omits `discarded-commits.md`; `SKILL.md:106` files table
  omits `tools/`; self-test runtime quoted as "7-20 min" at `README.md:1347`
  against "6.8-19.0" at 127 (B, *agent*).

## Where the documents and the code agree

Stated as a result, per the plan. The agents checked and found nothing against:
the tier defaults and `defaults:` override order; the four-source clock
resolution and its banner; the ledger splice and its refusal to touch another
step; the lock; the barren threshold and `on_wall`; the build-step budget path
and its `OVER BUDGET` semantics; the review verdict schema and `INCONCLUSIVE`;
worktree isolation, integration and `NEEDS MERGE` (in the README); the reflect
validation rules; `--mode`'s precedence. Axis A over the README returned five
lines from 1,383 lines of manual, and three of them are real.

---

## F. The efficiency objective is mis-stated - what it should say instead

**The bar as written** (`CLAUDE.md`, and `docs/token-efficiency.md:18-20`): an
unattended run must not be significantly more token-hungry than the same work
done interactively; 2x is not acceptable.

**Measured, it is met with room to spare and it measures the wrong thing.** Per
API call a headless worker costs what an interactive session costs (56/25/19
against 57/28/15); against a long interactive session it is roughly 2x
*cheaper*, by two independent denominators. The risk the bar defends against did
not materialise. Meanwhile the three ways a night actually fails are outside it:

- **the plan runs out** - the long session sat idle for 69% of its night having
  finished at 01:36; the runner has the same failure, reported as "the queue
  finished" with a timestamp nobody compares to the stop time;
- **a step is mis-cut** - `5b-report` ran 2.27x its estimate over two attempts;
  a step that carries two deliverables costs the square of its call count;
- **work is spent and not landed** - discarded attempts (9% of the FinKit run,
  a defect since fixed), reverts, reworks that fail, and sixteen `STUCK` steps
  in ten minutes at the wall.

None of those is a per-call number, so a per-call bar cannot see them.

**Proposed restatement.** Three objectives, each computable by `write_summary`
from the ledger the run already keeps, in this order of importance:

1. **Waste share of spend is bounded.** Split the run's cost into *landed* (the
   attempt whose commit is on the branch at the end), *judgement* (review,
   reflect, diagnostic - the deliberate price of running unsupervised, and not
   waste), and *waste* (attempts discarded, work reverted, reworks that failed,
   `STUCK` and `OVER BUDGET` steps, barren workers). The objective is a ceiling
   on the third bucket - **15% of spend** is the number the FinKit run suggests,
   where 9% was a defect and the rest was reworks. `overhead_line()` already
   splits building from not-building; this is one more cut of the same data.
2. **The night is filled.** Two deterministic signals that the plan was cut to
   the wrong size, both already in the log and neither summarised: the queue
   finishing more than an hour before the stop time (under-cut), and any step
   `NOT RUN` for the clock (over-cut, or badly ordered). The objective is zero
   of either; the report says which, and by how much.
3. **Each step sits on the floor of the U.** `docs/token-efficiency.md` found
   cost per call dear at 11 calls (a 55k prefix over eleven calls) and dear at
   100 (context grown to 119k), with a floor at **20-55 calls**, which is the 7-15
   minute step. The per-step objective is therefore not "short" or "long" but
   *in the band*: `tools/tally.py` already has each worker's call count and mean
   context; a step outside 15-80 calls is flagged in `SUMMARY.md` as mis-cut in
   the direction it missed. This is where Anthropic's guidance and ours meet:
   theirs (one long session beats several short ones, on cold starts) is the
   left arm of the curve, ours (context growth dominates, cold starts under 3%)
   is the right, and a plan is judged by how many steps it keeps between them.

**Retire the 2x-vs-interactive bar** to a footnote carrying its measurement. It
was the right question when nobody knew where the tokens went; it is answered.

**What this costs.** Objectives 1 and 2 need no new measurement - they are
arithmetic over `done:` blocks and the log. Objective 3 folds `tally.py`'s
per-worker numbers into `SUMMARY.md`, which is the roadmap item that report was
always going to become. All three are SM once the definitions above are agreed;
the definitions are the FH part and are this section.

---

## G. Where judgement lives, and what each decision shares a failure mode with

Every place the runner delegates a decision to a model, asked: *what happens to
the decision when the thing it is deciding about is what has failed?* Then:
does a deterministic check gate it?

| decision | delegated to | fails together with | deterministic gate today | gap |
|---|---|---|---|---|
| how to implement the step | build worker | the brief (mis-cut), the environment | gates; 3 attempts; `count_barren` | breaker cannot tell *step misconfigured* from *provider down* - **F2** |
| pass / rework / fail | review worker (opus/high) | its own outage - nothing else, and deliberately a different model from the builder | no typed verdict is `INCONCLUSIVE`, resumable | after `REVIEW REWORK FAILED` nothing gates what builds on the commit - **F5** |
| why two attempts failed | diagnostic worker (opus/high) | the same outage that made the attempts fail | none | spawned even when both attempts were barren, so it reads two empty transcripts and is the third barren worker itself (2479 has no barren check) |
| whether the plan still makes sense | reflect worker (opus/high) | the outage; and the plan's own shape, which is what it is judging | plan re-validated; changes outside the plan reverted | **a reflect whose worker never answered is recorded `REFLECT NO CHANGE`** - see G-4 |
| what the cut-off worker was doing | the next worker, from a transcript the runner compacted | the budget it already exhausted | `run.continuations` bounds it; "changed nothing, no handover" is deterministic | none found |
| is the account back | probe (haiku, "reply ok") | nothing semantic - its output is not trusted for content, only for existing | `alive = exit 0 and result` | probes haiku while the step needs sonnet or opus, so a single withdrawn model reads as alive - the environmental twin of **F2** |
| how the steps are cut | the planner, interactively | nothing at run time | `expected_min` required; gates | actual/expected, attempts and call count are the post-hoc signals - **F** objective 3 |
| what needs a person | `--mode`, deterministic | - | the right shape | its procedure is missing for one of four causes - **F6** |

**G-4, the generalised barren-reflect finding, verified.** `run_reflect`
(2619-2661) never inspects `code`. A reflect worker killed by the timeout, by the
stall watchdog, by a CLI error or by the wall leaves the plan untouched, and
"untouched" is recorded as `REFLECT NO CHANGE` - a benign outcome for the exit
code (3069), complete on resume, and in the morning reads as *the plan was
considered and found sound*. The wall case is caught only once the threshold
trips and the main loop discards the outcome (2999); the first two barren
reflects of a cascade, and every non-wall failure, are recorded as judgement.
This is the measured 2026-09-07 event (`reflect-3`, `NO CHANGE`, $0.00) in its
general form. **Fix:** a reflect with a non-zero exit and no result event is
`REFLECT INCONCLUSIVE` (the review already has this word and this rule), which
is resumable and not benign. One condition, no model.

**The principle the table shows.** Every model-made decision in the runner
already has a deterministic answer to "the model did not answer" - except the
reflect, and except that the breaker's answer is *park* when the cause is local
to one step. None of the gaps wants another model in the loop; Anthropic's
figure for multi-agent overhead is 3-10x, and every fix here removes a model
call or replaces one with a comparison Python can make:

- skip the diagnostic when both attempts were barren (there is nothing to read);
- stop re-parking when the probe answers and the step does not (F2b);
- record a reflect that did not run as not having run (G-4);
- hand a twice-failed judgement to a person rather than a third worker (F5a).

The roadmap's item 13 (attempt 2 gets the failed gate's name and output, not a
diagnostic) and item 14 (`git bisect` names the step that broke the suite, not
a worker) are the same principle already written down.

---

## Proposed fix set, grouped, with a tier per group

Nothing below is done. Each group is one commit with its self-test check shown
to fail first where the change is behavioural, `CHANGELOG.md` and `README.md` in
the same commit, full suite before it.

| group | contents | tier | why that tier |
|---|---|---|---|
| **1. Preflight refusals** | F1 (brief and preamble must exist); F2a (`effort` set, `run.budget_usd_per_step` positive); the two C warnings (`timeout_min <= expected_min`, missing `## Feeds`) | SM | one obvious shape each, copied from `expected_min` |
| **2. The breaker's blind spot** | F2b (probe alive + same step barren again = the step; record blocking, move on) | OM | one approach, but it touches the wall path and the resume queue, and the fixture has to prove the old runner loops |
| **3. Budget and gate plumbing** | F3 (`budget_for` at three call sites); F4 (`all_gates` in gate steps) | SM | mechanical |
| **4. Outcomes that lie** | G-4 (`REFLECT INCONCLUSIVE`); F5 (`REVIEW REWORK FAILED` blocking, docs corrected); skip the diagnostic over two barren attempts | OM | small code, but each is a contract change to the ledger and the exit code |
| **5. Untested paths** | T1-T6 fixtures, plus the three proxy checks (running step spans the clock, `OVER BUDGET` exit code, review runs when the clock is tight) | SM | fixtures against a fake worker; the fake may need one or two new behaviours |
| **6. Documents** | F6 (`NEEDS MERGE` in three files); F7 (two residues); every severity-2 and -3 wording line above | SL | wording |
| **7. The objective** | F: replace the `CLAUDE.md` paragraph and the head of `token-efficiency.md`; then the three `SUMMARY.md` lines | FH for the wording, SM for the lines | the wording is the decision; the lines are arithmetic |

Groups 1, 3 and 6 are safe to do in one sitting. Group 2 and group 4 change
what a resume does and should each be read as a diff before the suite runs.
Group 7 waits on Paul's yes to section F as written or amended.
