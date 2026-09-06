# Field report 2: the fixes under real conditions

FinKit, run `stage-3b-5b`, 2026-09-06 pm. Skill at `e88859c` - the four fixes
from field report 1. SELFTEST PASS.

    --only 3c-flow-denominator-required,3c-annual-aggregation-by-unit,
           review:3c-annual-aggregation-by-unit --hours 2

Exit 0, 3 of 3 PASS, $12.90. **Second consecutive session with zero
runner-caused failures.**

## The four fixes from report 1

- **Worktree isolation (`7d5b801`) - PROVEN.** Both build steps isolated cleanly,
  the work landed back on `main`, no `clean_tree` failure. Worktrees are placed
  in a SIBLING directory (`FinKit.overnight-worktrees/`), not inside the repo, so
  a worktree cannot itself appear as an untracked path in the tree it protects.
  Not obvious, and load-bearing.
- **The gate-failure message (`474639a`) - still unproven.** Nothing failed a
  gate.
- **Reflect/diagnostic `bypassPermissions` (`474639a`) - still unproven.** No
  reflect step in this run and no step failed twice. Report 1's evidence for
  wanting both stands; the fixes themselves have not been exercised in the field.
  Both ARE covered by the self-test, which fails a step deliberately.
- **Quiet heartbeat and run cost total (`3e15329`) - both worked.** The
  orchestrating session went from about a turn a minute to a turn a step.

## Defect 4 - the refusal that has to teach

Not a defect in the runner: a defect the runner FOUND, and the most valuable
thing it did all day.

Because build steps now run in a worktree, gates execute against a FRESH CHECKOUT
rather than the operator's working copy. Probed by hand before launching, the
FinKit suite FAILED there - 2 failed, 728 passed - while passing 740 in the
working copy. Both were real defects the stale working copy had been hiding for
weeks:

- a test converted an LF fixture to CRLF to prove line endings are not a
  difference. `core.autocrlf` handed a fresh checkout the file ALREADY converted,
  so the conversion was a no-op and the test failed on its own precondition.
  Fixed with `tests/fixtures/** -text`.
- a test read a gitignored generated report, absent in any clean checkout. Fixed
  by generating it when missing - deliberately NOT a skip, because a guard that
  stops running in the environment the workers use is not a guard.

**The implication for the skill, and it is the headline.** Switching the default
to worktree silently raises the bar on every project that adopts it, from "the
suite passes here" to "the suite passes from a clone". That is the right bar. But
a project that has never been cloned discovers it at preflight, as a refusal,
with no explanation of why gates that pass in the shell fail in the runner - and
the natural response is to set `in-place`, which costs it exactly the protection
of defect 1's fix.

**Acted on:** the failing preflight message now names the three usual causes in
the order they turn up - a generated or gitignored artefact, a fixture git
converts on checkout, state git does not carry at all - says that the first two
are usually real defects the working copy was hiding, and says plainly that
`in-place` is the fallback and not the first answer.

## The review -> rework loop is the best thing in the runner

First real evidence. `review:3c-annual-aggregation-by-unit` returned REWORK, 0
blockers and 2 majors, and both findings were real - verified independently
rather than taken on trust:

- the build handled 8 reference-parameter keys out of 39; the other 31 fell
  through a pass-through branch and would have reached an annual row still naming
  the MONTHLY parent, silently.
- a units predicate returned True for both `pct/year` and `pct/month`, which
  cannot both be right. The reviewer called it a misleading message; it was
  actually a wrong predicate. **The reviewer UNDER-called it.**

The reviewer also re-ran the gates itself rather than reading them, and verified a
"does this new test actually bite?" claim by checking out the PARENT commit and
confirming 5 of 7 new tests fail there. That is "a gate is a floor, not a
standard" applied without being told.

Its arithmetic in prose drifted: it said 8 of 41 and 33 unhandled where the truth
was 8 of 39 and 31. Immaterial here, but a count is the part of a finding an
operator acts on without re-deriving it, and nothing downstream checks a
reviewer's prose. **Acted on:** the review brief now says to COUNT and not
estimate - every number in a finding must come from a command that was run.

## Rework economics (n=1, but the shape is useful)

| | wall clock | turns | cost |
|---|---|---|---|
| build | 17.4 min | 70 | $5.49 |
| rework | 13.1 min | 31 | $2.34 |
| rework as a fraction | 75% | 44% | 43% |

Turns fall much faster than minutes, because the rework branches from the BUILD
commit and keeps both the code and the findings. **Budget a review + rework at
about 1.5x the build, not 2x.**

## Smaller notes

- Cost per step tracked the estimate well: 12 min estimated against 7.7 actual,
  18 against 17.4. `expected_min` is earning its place.
- The final line totals the LEDGER - every step the plan has ever run - not this
  session, and the wording read as the latter. **Acted on:** it now prints both,
  each said to be what it is.
- `--only` naming a review step can cost more than it looks: a `rework` verdict
  spawns a full build worker that was never named. **Acted on** in the README
  flag table.
- Commit attribution is still unreliable - a sonnet-spawned worker produced a
  trailer crediting Opus. `3e15329` records `tier:` in the ledger, which is the
  right fix; the trailer itself should not be trusted or parsed.
- The ledger's `done:` blocks now carry enough that `--mode` alone was a
  sufficient handoff after a `/clear`.

## Bottom line

Worktree isolation is proven. The remaining unproven paths are the gate-failure
message and the reflect/diagnostic permission fix - both need a step that
actually fails, which no real run has produced since they landed. The self-test
does produce one, deliberately, and asserts both; what is missing is a real
worker meeting them, not a guard.
