# Audit findings - the implementation against the theory

Written 2026-09-08, executing `plan-architecture-audit.md` at FH. Axes A to E
were run as six Sonnet subagents in one parallel batch, each with an explicit
file list and a pipe-delimited output contract capped at 25 lines; they returned
70 raw lines in total (A1 5, A2 5, B 10, C 13, D 18, E 19). Axes F and G were
done in the main context with the findings in hand.

**Every severity-1 line was verified by reading the code before it appears
here.** Lines marked *agent* are severity 2 or 3 and are carried as reported.
Where a subagent's severity did not survive verification the change is stated.
The proposed fix set is at the end, and the ordering it is built in is the
appendix after that.

**Status.** Nothing here was fixed when it was written.

- **Phase 1** (the document harms: F7, F6, the `run:` key table, the severity-3
  wording) landed 2026-09-08.
- **Phase 2** (the barren family: the `is_barren` extraction, G-4, the skipped
  diagnostic, F2b) landed 2026-09-08. So **F2's second layer and G-4 are fixed**;
  F2's first layer, the load-time refusal of a bad `effort` or budget, is phase 3
  and still stands.
- **Phase 3** (F1, F2's first layer, A2) landed 2026-09-09. **F1, F2 and A2 are
  fixed.** `preflight` now stats every still-to-run build step's `brief` and
  `run.preamble` before doing anything else, and refuses with the full list of
  missing paths. `load_spec` refuses a step's or `run.defaults.<kind>`'s
  `effort` outside `low`/`medium`/`high`, and a non-positive
  `run.budget_usd_per_step` - `model` stays unchecked, as the audit recommends.
  `load_spec` also refuses a build step whose `timeout_min` (its own, or
  `run.worker_timeout_min`) cannot outlast its own `expected_min`, collected
  into one message the same way an unsized step is.
- **Phase 4a** (F3) landed 2026-09-09. **F3 is fixed.** `run_review`,
  `run_reflect` and `run_diagnostic` now pass `budget=self.budget_for(step)`
  to `worker_argv`, matching the build path - a step's own `budget_usd`
  reaches its worker instead of the run-level cap applying silently.
- **Phase 4b** (F4, T3) landed 2026-09-09. **F4 is fixed.** `run_gate_step`
  now runs `self.all_gates(step)` instead of the step's own `gates` alone, so
  a `kind: gate` checkpoint runs the universal gates too, matching what the
  README already said. T3 (new section 26) proves the defect first: a
  universal gate that could not itself fail preflight was invisible in a
  checkpoint's `gates.log` before the fix and present after it.
- **Phase 4c** (F5) landed 2026-09-09, per Paul's 2026-09-08 decision: blocking,
  not resumed. **F5 is fixed.** `"REVIEW REWORK FAILED"` is now in
  `BLOCKING_OUTCOMES`; the dead, unreachable bare `"REWORK FAILED"` is removed
  from `RERUN_OUTCOMES` (it never reached the ledger under that name - see the
  finding text below); `README.md`, `references/spec-format.md` and
  `references/blocked.md` now document it as blocking and complete, not
  re-run. T5 (new section 27, 5 checks) proves it end to end: a rework attempt
  that fails its own gates records `REVIEW REWORK FAILED`, `--mode` reports
  `BLOCKED`, the step is not resumable, and the run's own exit code is
  non-zero.

  **A related question surfaced while writing T5, out of this phase's
  scope.** The reset that follows a failed rework rolls the branch back to
  the REVIEWED commit - correctly, that is what leaves "the reviewed commit
  still stands" true - but that reviewed commit sits BEFORE the ledger splice
  that recorded the underlying build step's own `done: PASS`. The reset
  discards that splice commit too (tagged `rescue/<step>-rework/N`, not
  lost), so the build step's `done:` block is gone from the spec afterward,
  not the review's. A bare relaunch (skipping the skill's `--mode` gate, which
  is where `BLOCKING_OUTCOMES` is actually enforced today - the runner itself
  does not consult it) would see the build step as still-to-run and rebuild it
  from scratch, while the blocking review step still names the OLD sha. Not
  investigated further; flagged for Paul's judgement on whether it is worth a
  fix and what shape one would take.
- **Phase 5** (T1, T2, T4, T6, and the three proxy-coverage upgrades) landed
  2026-09-09. Changes no behaviour; adds coverage only. T1: `fake_worker.py`
  exits 99 if any `STRIP_ENV` name reaches it (new section 28). T2: `on_fail:
  record` and `on_fail: revert` each get a fixture (new section 29). T4:
  `cmd_empty` and `fresh_shell` gate forms, both passing and failing (new
  section 30). T6: `--rerun` actually spawns a second worker for an
  already-PASSed step, proven via the fake worker's own call counter, not
  just a benign exit code (new section 31). The three proxy upgrades: a
  RUNNING step spans the stop time and the step after it is not started (new
  section 32 - needed its own lightweight spec, since `SPEC`'s universal
  `python -m pytest -q` gate alone ate the whole timing window on the first
  attempt); `OVER BUDGET`'s exit code is 1 (new section 33); `choose()` never
  declines a review for the clock, called directly with the stop time an hour
  in the past rather than timed through a subprocess (new section 34).
- **Phase 6** (section F, the efficiency objective) landed 2026-09-09,
  genuinely last as planned - it changes the outcome vocabulary phases 2 and
  4 already settled. The 2x-vs-interactive bar is retired to a footnote in
  `docs/token-efficiency.md` carrying its measurement; `CLAUDE.md` states the
  three objectives agreed 2026-09-08 instead. `overhead_line()` is replaced
  by three methods, each a new line in `SUMMARY.md`: `waste_share_line()`
  (landed/judgement/waste, a new `WASTE_OUTCOMES` set, 20% ceiling, reported
  and monitored, never gated), `night_filled_line()` (under-cut if the queue
  finished over an hour early, over-cut if the clock declined to start a
  step - reads the `reason` `write_summary` already carries), `step_size_line()`
  (a build step outside 15-80 API calls, counted by a new `count_api_calls()`
  helper reading the step's own logs, mirroring `tools/tally.py`'s
  call-counting without importing it). Tested directly against a mock
  Runner rather than through a live run (new sections 35-37, 12 checks) - the
  fake worker's logs carry no per-call usage records at all outside the
  `pass-rewoken` fixture, so unit-testing the pure functions is both cheaper
  and more deterministic than orchestrating specific outcome combinations
  through a subprocess. One of the three (section 35) also proves the
  wiring against a real run's `SUMMARY.md`, not just the functions in
  isolation.

Everything else stands.

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

**DECIDED 2026-09-08, by Paul: (a), blocking.** A review whose rework failed
stops the run for a person. Phase 4c below is unblocked and builds that.

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
| T5 | `REFLECT NO CHANGE` (2661), `REVIEW REWORK FAILED` (2599), bare `REWORK FAILED` (2489) | none of the three strings | see F5 and G-4. **Half closed in phase 2**: section 15 now covers the barren reflect, so `REFLECT NO CHANGE` has a neighbour that pins it. The two review strings are still untested and are phase 4c |
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

**AGREED 2026-09-08, by Paul: this section stands as written.** The three
objectives below replace the 2x bar, which retires to a footnote carrying its
measurement. Phase 6 builds it, and stays last: phases 2 and 4 change the
outcome vocabulary objective 1 partitions, so a classifier written before them
is written twice.

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
   on the third bucket - **20% of spend** (Paul, 2026-09-08, raising the 15% the
   FinKit run suggested, where 9% was a defect and the rest was reworks).
   `overhead_line()` already splits building from not-building; this is one more
   cut of the same data.

   **A breach is REPORTED AND MONITORED, not failed** (Paul, 2026-09-08). It
   prints in `SUMMARY.md` as a number against the ceiling and is watched across
   runs; it does not change an exit code, refuse a plan or gate anything. The
   reason is what the night is actually for: **an unsupervised run that is
   effective, even at some waste, against an eight-hour interactive session at
   opus/high.** Measured, the runner costs about half per API call and is
   working while nobody is awake, so a run that lands its plan at 25% waste has
   still beaten the alternative comfortably - and a bar that failed it would
   push a planner toward timid steps and fewer reviews, which is the expensive
   direction. The ceiling exists to catch a run that is wasting *structurally*,
   over several nights, not to grade one.
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
Group 7 has Paul's yes to section F as written (2026-09-08); it waits on phases
2 and 4 rather than on a decision.

---

# The build plan - ordering, and whether to run it overnight

Added 2026-09-08 at OH, reviewing the seven groups above rather than restating
them. Four of them change on a second reading, and the fourth change reorders
the plan more than any other observation in this document.

## Amendments to the fix set above

**A1. Drop the `## Feeds` preflight check entirely.** It was proposed as a
warning, and a warning printed into `run.log` at 23:00 is read by nobody - which
is the whole argument axis C makes against advisory rules. So it must either
refuse or not exist. Refusing a night because a brief states its downstream
contract without that exact heading is far too strong, and it would train
planners to paste a heading rather than write a contract. It stays advisory in
`planning.md` deliberately, and that is the honest answer rather than a
half-measure.

**A2. `timeout_min <= expected_min` refuses; it does not warn.** Same test,
opposite conclusion. A timeout at or below the step's own estimate is
arithmetically nonsense: a step that runs to the length its planner predicted is
guaranteed to be killed mid-work. There is no legitimate spec that does this, so
it is refused at load, collected into the message `expected_min` already builds.

**A3. The `run:` key table comes out of the documents group.** Three keys missing
from a table `spec-format.md:4` calls exhaustive is not a wording slip when the
three are `isolation`, `worktree_link` and `worktree_root` - the controls for the
defence 1.0.3 made the default. The fresh-worktree refusal message
(`overnight.py:2904`) tells the user to "list the paths under
`run.worktree_link:`", and the manual has no such entry. It needs a row each and
a paragraph, not a one-line correction.

**A4. G-4, the diagnostic skip and F2b are one piece, not three.** All three ask
the same question - *did this worker return nothing?* - which `count_barren`
already answers privately at `overnight.py:1866`. Fixed as separate steps they
each grow a copy of that condition and the third refactors the first two. This is
the largest single rework hazard in the set, and it is why the ordering below
does not follow the group numbering.

## The ordering

Dependency and rework first, impact second within each phase.

### Phase 1 - stop the live harm. SL/SM. No code, no test, no prerequisite.

First because it depends on nothing and because it is the only finding
**currently costing nights**. `references/spec-format.md:45` and
`examples/steps.example.yaml:37` still show the full suite as a universal gate,
annotated "appended to EVERY build step" - 1.0.3 corrected `planning.md` and the
README and left the two files a planner actually opens, one for the key format
and one to copy the shape from.

- **1a.** Both files' `run.gates` become cheap and local; the example grows a
  `kind: gate` checkpoint carrying the suite, which is the shape the README now
  recommends anyway.
- **1b.** `NEEDS MERGE` as the fourth BLOCKED cause in `SKILL.md:28` and
  `spec-format.md:184`, with a recovery section in `references/blocked.md`.
- **1c.** A3, the `run:` key table.
- **1d.** The severity-3 wording that describes behaviour nothing later changes.
  **Hold back** the resume-rule wording at `README.md:761` - it moves in phase 4.

No runner change, so no self-test change; say that in the commit message, per the
project rule.

### Phase 2 - the barren family, as one coherent piece. OM.

Second because of A4: this is where separate steps cost the most, and it is the
highest-value code change in the set.

- **2a.** Extract the barren predicate out of `count_barren`. A pure refactor,
  and the suite passing unchanged is the proof that it is one.
- **2b.** G-4: a reflect whose worker returned nothing becomes
  `REFLECT INCONCLUSIVE` - into `RERUN_OUTCOMES`, out of the benign set at 3068.
  Fixture first: today that scenario yields `REFLECT NO CHANGE` and exit 0.
- **2c.** Skip the diagnostic when both attempts were barren (`overnight.py:2479`).
  There is nothing in those transcripts to read, and it is the third barren
  worker of the cascade.
- **2d.** F2b: when the probe answers and the same step goes barren to threshold
  again, it is the step, not the environment. Record blocking with the argv and
  the stderr tail; take the next step instead of parking again.

**Ordering constraint inside the phase:** 2d's fixture must induce barrenness
through the fake-worker scenario, never through a bad `effort` in the spec, or
phase 3 makes that fixture unbuildable.

Gates: `--only 15` and `--only 1` while working; the full suite closes the phase.

### Phase 3 - refuse it at load or at preflight. SM.

Cheap once the shape is copied from `expected_min`, and after 2d for the reason
just given.

- **3a.** F1: stat every still-to-run build step's brief and `run.preamble` at
  preflight, collect the offenders into one message, exit 2 before the lock.
  **Verified safe against the current suite:** `scaffold` (`selftest.py:340-353`)
  writes `_preamble.md` and s1-s3 into every fixture repository, so no existing
  fixture depends on a missing brief.
- **3b.** F2a: `effort` in {low, medium, high}; `run.budget_usd_per_step` through
  the positive-number check its per-step twin already gets.
- **3c.** A2, the timeout refusal.

Gate: `--only 25`, which already owns load-time refusals.

### Phase 4 - the plumbing, then the contract. SM, SM, OM.

- **4a.** F3: `budget=self.budget_for(step)` at the review, reflect and
  diagnostic call sites. Asserted on the argv line of `review.log`, which
  section 1 already reads at `selftest.py:494`.
- **4b.** F4: `self.all_gates(step)` in `run_gate_step`, with the `kind: gate`
  fixture (T3) written first and shown to fail.
- **4c.** F5: `REVIEW REWORK FAILED` into `BLOCKING_OUTCOMES`, the unreachable
  bare `REWORK FAILED` resolved, `blocked.md` and the two resume-rule statements
  corrected in the same commit. **Decided 2026-09-08: blocking.** A judgement
  that failed twice goes to a person, not to a third worker; `blocked.md` gains
  a fifth cause and the README's resume rules stop promising a re-run.

### Phase 5 - the remaining coverage. SM. Changes no behaviour; nothing depends on it.

**T1 first within the phase**, because it guards the only finding that costs
money rather than time: one line in `fake_worker.py` exiting non-zero if any
`STRIP_ENV` name survives into the child, and one check. Then T2
(`on_fail: record` and `revert`), T4 (`cmd_empty`, `fresh_shell`), T6
(`--rerun`), and last the three proxy upgrades.

### Phase 6 - the objective. SM throughout now. Genuinely last.

Not merely by preference. Objective 1 partitions the outcome vocabulary into
landed, judgement and waste - and phases 2 and 4 **change that vocabulary**:
`REFLECT INCONCLUSIVE` arrives, `REVIEW REWORK FAILED` changes class. A
classifier written before them is written twice. Objectives 2 and 3 carry no such
dependency but belong in the commit with the wording they serve.

**The FH half is already spent.** It was writing the definitions, and section F
is those definitions, agreed on 2026-09-08. What is left is transcription -
replacing the `CLAUDE.md` paragraph and the head of `docs/token-efficiency.md`
with what section F says, the 2x bar retired to a footnote carrying its
measurement - and arithmetic over the ledger for the three `SUMMARY.md` lines.
Both are SM. The ceiling in objective 1 is settled at 20%, reported and
monitored rather than enforced.

| phase | what | tier | blocked on |
|---|---|---|---|
| 1 | the live document harms | SL/SM | nothing |
| 2 | the barren family, one piece | OM | nothing |
| 3 | load and preflight refusals | SM | 2d's fixture shape |
| 4a-4b | budget and gate plumbing | SM | nothing |
| 4c | `REVIEW REWORK FAILED` -> blocking | OM | decided 2026-09-08 |
| 5 | remaining coverage | SM | nothing |
| 6 | the objective | SM | phases 2 and 4 (the decision came 2026-09-08) |

## Should the overnight skill implement this?

**Yes for phases 1, 3, 4a-4b and 5. No for phase 2. Phase 6 not at all.**

**The obvious objection dissolves.** The workers would be editing the runner that
is running them, and each step's gate spawns the very file being edited - which
is the failure that cost twenty minutes on 2026-09-08. Worktree isolation, the
default, answers it: each worker edits its own checkout and runs the suite there;
the orchestrator loaded `overnight.py` at process start and never reloads it; the
next worker's worktree is cut from the integrated HEAD and so carries its
predecessor's work. This is the one project where that property is load-bearing
rather than incidental. **`run.isolation: in-place` must not be set**, and the
worktree preflight probe will say so before any worker starts.

**The gate must be narrow, and this plan makes that easy.** The full suite
measured 12.4 minutes tonight; twelve steps gated on it is two and a half hours
spent re-proving finished work, which is exactly what 1.0.3 told planners not to
do. Every phase above names the sections covering it, so each step gates on
`selftest.py --only <n>` plus `clean_tree`, and a `kind: gate` checkpoint
carrying the full suite closes each phase. Four checkpoints, about fifty minutes,
against a hundred and fifty. Note the mild recursion: until 4b lands a checkpoint
runs only its own gates, so make the full suite the checkpoint's **own** gate
rather than relying on `run.gates`.

**What must stay out of the run:**

- **Phase 2.** Its four substeps share one predicate and one mental model, and
  four fresh workers each re-deriving "what does barren mean here" is precisely
  the decomposition Anthropic's guidance warns against - by context boundary, not
  by problem type. It is also the highest blast radius in the set: it alters the
  wall path, where a subtle error mis-records a night instead of failing loudly.
  Do it interactively at OM. If it must go overnight it is **one** step, not
  four, at `sonnet/high`, with 2d split off as a second.
- **Phase 4c** was held back until the blocking-or-resumable question was
  answered, on the grounds that a worker will not settle it at 3am. It was
  answered on 2026-09-08 (blocking), so it may now go in the run - as its own
  step, with the decision stated in the brief rather than left to the worker.
- **Phase 6**, but the reason has changed. It is no longer that the wording is an
  unmade judgement - section F is agreed. It is the dependency: objective 1
  classifies outcomes that phase 2 and phase 4c redefine, and phase 2 is not in
  the run. Phase 6 goes in the run AFTER the one that lands phase 2.

**What the run buys beyond the work itself.** It is the first ledger under the
sonnet-default tiers, so one grep of its logs settles the `costBasis` 1.5x
question left open last session. It exercises `kind: gate` checkpoints in anger,
which nothing ever has (T3). And it produces exactly the run-log evidence section
F's objectives are defined over, from a plan whose steps were sized by somebody
who had just read the code.

**Sizing.** Eleven to thirteen build steps from the substeps above, plus a review
per phase and four checkpoints: three to four hours of building, comfortably
inside a night, with every step in the 20-55 call band section F calls the floor
of the curve.

**One prerequisite.** The plan is written into this project's own `overnight/`,
which does not exist yet. And while the run is live, nothing else edits these
files - the run commits to the branch as it goes, which is what it is for, but a
second interactive session in the same tree is what hard constraint 1 forbids.
