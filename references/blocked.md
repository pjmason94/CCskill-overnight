# BLOCKED - a step needs a person

`--mode` printed `BLOCKED`. Something in the plan cannot be resolved by this
skill or by the runner, and the run does not go further until the user decides.

**Do not do anything else.** Do not offer to resume, do not offer to re-plan, do
not start a worker, do not edit the plan. Report it and stop. The value of this
mode is entirely in the quality of the report.

---

## Why it stops

**STUCK** - the step ran up to three times, failed its gates every time, and a
diagnostic worker read a transcript of the first two attempts and wrote a
remediation plan that the third attempt ingested and still failed. Every retry
the runner has has been spent. A fourth attempt on the same brief against the
same gates is not a plan, it is a hope.

**HALTED** - a third party committed to the branch during the step, so resetting
the tree would have destroyed somebody else's work. The runner refused, which is
correct, but it means the step's own state was never cleaned up and the branch
now holds commits from two sources.

**OVER BUDGET** - the worker was cut off part-way by `run.budget_usd_per_step`,
having spent the whole cap. It is deliberately not retried: the cap is per worker
invocation, so a second attempt buys the same cut-off at the same price. Nothing
here says the worker was wrong - it says the brief asks for more than the cap
will pay for, or the cap is set below what the work costs, and only the user can
say which.

All three mean the ground is wrong, not that the step was unlucky. Finding that
out at 09:00 costs one step; not finding out costs the four steps built on top
of it.

## What to report

Read these and put them in front of the user, in this order. Do not make them go
and find any of it.

1. **The step**: its id, title, outcome, when it ran and for how long against its
   `expected_min`. All of it is in the `done:` block in `overnight/steps.yaml`.
2. **For STUCK - the gate it failed**, from the `note:` in `done:`, and then
   **the whole of `overnight/runs/<run>/<step>/remediation.md`**. The diagnostic
   already worked out what it thought was wrong; summarise its finding in a
   sentence and quote the part that matters.
3. **For HALTED - the foreign commit**, from
   `overnight/runs/<run>/<step>/discarded-commits.md`: its sha, who made it and
   its subject line. Say plainly that the tree was NOT reset and that the branch
   holds both the run's commits and theirs.
4. **For OVER BUDGET - the two figures in the `note:`**: what the worker spent
   and what the cap was, and what the step's other attempts (if any) cost. Then
   say which of the two choices the user is being asked to make - split the step
   into smaller ones, or raise `run.budget_usd_per_step` - and, if the run's
   other steps have costs recorded, what a step of this size has actually been
   costing. Do not recommend raising the cap without that number.
5. **Anything that step wrote to `overnight/DECISIONS-PENDING.md`.** Workers are
   told to write findings there as they learn them, so a step that got into
   trouble has usually said why.
6. **What else was in flight**: how many steps remain, and whether any of them
   depend on this one. A stuck step that nothing depends on is a different
   conversation from one that four steps build on.

## The options, as commands the user runs

Give these as commands, not as offers to act. The user resolves this, not you.

    # retry just that step, after fixing the ground or the brief
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --only <id>

    # accept it and carry on with the rest, deliberately
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --from <next id>

    # forget every recorded outcome - this launches nothing, it only forgets
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --reset-state

    # ...and then, as a separate command, start the plan again
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml

For HALTED, add: sort the branch out first - `git log`, decide what to keep, and
either keep the foreign commit or move it aside - because until the branch is one
person's again the next reset will refuse in the same way.

Editing the brief and retrying, or cutting the step from the plan by hand, are
both fine and both the user's call. Offer your reading of which is right, in one
line, with the reason - then let them choose.

## What blocking costs, and why it is still right

One STUCK step blocks the whole plan, including steps that do not depend on it
and would have run perfectly well. Say so if it applies, and name the `--from`
command that overrides it. The trade is deliberate: a stuck step is usually a
signal that the plan was wrong, and building four more steps on a wrong plan
overnight is the expensive outcome, not the lost hours.
