# Design: triaging a judgement call instead of always stopping

Roadmap item 1. Written 2026-09-06, before any of the code.

## The hook does not exist yet

Read this first, because it changes what "building item 1" means. There is no
structured way for a worker to raise a judgement today. `DECISIONS-PENDING.md` is
free text, and the instruction to write it lives in each project's own preamble,
not in the runner. The runner never reads it. So item 1 is two builds: the
raising, then the triage. The raising is the smaller half and the more useful one
- a project gets value from structured decisions in the morning report even if
the triage is never turned on.

## Stage A - raising

Build workers get a JSON schema, the way review and reflect workers already do,
carrying the summary plus:

    decisions: [{
      question,        # one line: the fork, not the background
      options: [{label, why, reversible}],      # 2 to 4
      recommended,     # which option, if it had to choose
      confidence,      # 0..1, its own P(recommended is right)
      detectable,      # "diff" | "gate" | "review" | "hidden"
      blocked          # true if it could not finish the step without an answer
    }]

The instruction moves out of the project preamble and into the runner's build
brief: *if you hit a fork you cannot settle from the brief and the repository,
raise it - do not guess silently, and do not stop if you can do the rest.*

`detectable` is the field to get right, and workers will over-rate it. The brief
must define it by example: `diff` = a reader of the commit sees it; `gate` = a
gate fails; `review` = the reviewer would catch it reading the commit; `hidden` =
it would pass every gate and read correctly and still be wrong.

## Stage B - the triage, and the arithmetic that does not work

After a build step records, each raised decision gets one triage worker call:
the decision, the pending plan, the computed fanout, the run's confidence product
so far, and the detectability rule. It returns a choice, a confidence, a
rationale, and a proposed step - never an edit.

**The EV rule as stated in the roadmap is degenerate, and the code must not
implement it literally.** Write out what the roadmap proposes:

    proceed if   P(wrong) x rework_cost   <   time saved by not stopping

Rework cost is the dependent subtree redone: `p x m x S`, for subtree minutes `S`
and a reintegration multiplier `m`. But the roadmap also says, correctly, that
stopping does not abort the run - it skips the step, and what is stranded is
exactly that same dependent subtree, `S`. So the comparison is

    p x m x S   <   S      =>      p x m < 1

and `S` cancels. The fanout - the whole point of refinement 1 - drops out of the
arithmetic entirely, and what is left is a bare confidence threshold. Any
implementation that carries `S` on both sides is doing arithmetic that cannot
change its own answer, which is worse than doing none: it looks principled.

**The honest model, and where the fanout really enters.**

1. **Detectability is a GATE, not a term.** If `detectable == "hidden"`, never
   take the call, at any confidence, for any saving. An error that no diff, no
   gate and no review would reveal has no bounded cost, so no finite time saving
   pays for it. This is refinement 3, and it belongs before the arithmetic rather
   than inside it.
2. **The fanout enters through REINTEGRATION, not through raw minutes.** Redoing
   one step costs that step. Redoing a step that eight others have since been
   built on costs those eight and the work of reconciling them - and that second
   term does not cancel, because stopping never incurs it. So

       m = 1 + reintegration_per_step x (fanout_count - 1)      # capped, say at 4
       proceed if   p_wrong x m   <   delay_weight

   `delay_weight` (default 0.5) is what a night's delay is worth relative to the
   work itself - the one honest preference in the model, and it belongs in
   `run:`, not in a prompt. This reproduces the roadmap's intuition - the same
   call at the same confidence is right to take late and wrong to take early -
   without pretending the minutes drive it.
3. **The budget is a hard stop over the top.** Keep the run's confidence product.
   If taking this decision would put it below `min_run_confidence` (default 0.5),
   defer it whatever the arithmetic says, and log that the budget is what stopped
   it. Ten calls at 90% is 0.35 that the whole chain is sound.

**The runner does the arithmetic; the LLM supplies only estimates.** The triage
worker returns `confidence` and `detectable`; the runner computes `m`, applies
the thresholds, and logs the comparison with its numbers. Arithmetic done in
prose is arithmetic done badly, and a threshold the runner owns is one a person
can audit in `run.log` in the morning.

**Fanout needs a dependency notion.** There is none today. Add optional
`needs: [ids]` to a step, use its transitive closure when present, and fall back
to "every pending step after this one" when absent. The fallback over-counts,
which raises `m`, which defers more decisions to the human - erring in the safe
direction, which is the only acceptable direction for a default.

## Stage C - injection

If the decision is taken, the triage's proposed step is spliced into the plan
after the current one: id `decided-<n>-<slug>`, kind build, with a brief file
written beside the existing briefs. It is validated by **the same code path a
reflect's plan change is validated by** - completed steps unchanged, valid YAML,
the brief exists - and committed as `overnight: decision <id> taken`. Then it
faces the gates and the review like any other step. The triage never edits code.

## Stage D - the record

Every decision, taken or deferred, is appended to `<out>/decisions.json` (durable,
machine-readable, and what the confidence product is recomputed from on resume)
and in human form to `DECISIONS-PENDING.md`. `SUMMARY.md` LEADS with them, before
the step table: the question, the choice, the confidence, the option rejected, one
line of why, and whether a person still needs to look. A deferred decision is the
morning's first job; a taken one is the thing most worth checking.

On resume, decisions already recorded are not re-triaged - key them by step id
plus a hash of the question.

## Caps

One triage call per decision is a real cost and a real risk of a runaway. Cap at
`max_decisions_per_step` (2) and `max_decisions_per_run` (6); past the cap,
everything defers to the morning with that as the reason.

## Self-test coverage this needs

1. A raised decision with `detectable: hidden` is NEVER taken, at confidence 0.99,
   with a fanout of one. The gate beats the arithmetic.
2. The same decision at `detectable: diff` and a fanout of one is taken; at a
   fanout of eight it is deferred. That is the fanout doing work, which is the
   thing the degenerate model could not do.
3. The confidence product crossing `min_run_confidence` defers the next decision
   and says the budget stopped it.
4. An injected step appears after the current step, has a brief on disk, runs,
   and passes its gates - and a reflect step afterwards is still refused if it
   touches a completed step, i.e. injection did not weaken the validation.
5. `decisions.json` survives a resume and the same decision is not triaged twice.
6. `SUMMARY.md` leads with the decisions.
7. The caps hold.

## Order of work

Stage A alone, shipped and used for a night, is worth more than the whole thing
built at once: it turns "the workers raised some things, they are in a text file
somewhere" into a structured list in the morning report. Build A, run a night on
it, then build B/C/D against real raised decisions rather than invented ones.
