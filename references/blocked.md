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

**OVER BUDGET** - the worker was cut off part-way by its budget cap (the step's
own `budget_usd`, else `run.budget_usd_per_step`), having spent the whole thing,
and either continuation is off, or its continuations were used up, or it had
changed NOTHING in the tree and there was nothing to hand on. It is deliberately
not retried: the cap is per worker invocation, so a second attempt buys the same
cut-off at the same price. Nothing here says the worker was wrong - it says the
brief asks for more than the cap will pay for, or the cap is set below what the
work costs, and only the user can say which. **The note says which of the three
it was, and that changes the advice:** used-up continuations means the step is
too big for its cap, while "changed NOTHING" means a worker went nowhere for a
whole cap, which is a runaway and a reason to look at the brief itself.

The first three mean the ground is wrong, not that the step was unlucky. Finding
that out at 09:00 costs one step; not finding out costs the four steps built on
top of it.

**NEEDS MERGE** - the step's work is finished and good, and it is not on your
branch. Under worktree isolation a build step commits on its own scratch branch,
`overnight/<run name>/<step id>`, and is integrated onto your branch once its
gates pass. When that replay will not land - a conflict, or commits an earlier
crashed run left behind that do not rebase - the runner keeps the commits where
they are and stops. This one is different in kind from the three above: nothing
is wrong with the work or the ground. The work exists, it passed, and only a
person can say how it should land. The run stops rather than carrying on because
every later step would otherwise build on a tree the plan did not intend.

**REVIEW REWORK FAILED** - a reviewer found the committed step's work wanting,
one more build attempt tried to fix it on top of that commit, and the fix
attempt failed its own gates. The reviewed commit still stands - nothing was
reverted, nothing was reworked into it - and the run stops rather than
building on top of a commit a reviewer flagged and a rework already failed to
repair: a person decides whether the original findings still matter before
anything else depends on it.

**BARREN** - this step's workers would not start. They exited non-zero having
produced no result event at all, `run.wall_threshold` in a row (three by
default), twice over - and between the two the runner's probe got an answer, so
the account was up. That is the signature of something local to the step that the
CLI rejects before it makes an API call: a `model` it does not have, an `effort`
outside `low|medium|high`, a budget that is not a number. It can also be a model
withdrawn while the rest of the account still answers. Nothing was learned about
the work, and the run took the next step rather than parking on this one all
night.

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
4. **For OVER BUDGET - the two figures in the `note:`**, what the worker spent
   and what the cap was, plus `legs:` if the step took more than one worker.
   Then say which of the three choices the user is being asked to make - split
   the step into smaller ones, raise its `budget_usd`, or allow more
   `run.continuations` - and, if the run's other steps have costs recorded, what
   a step of this size has actually been costing. Do not recommend raising the
   cap without that number. If the note says the worker **changed NOTHING**, say
   so plainly and do not recommend more budget at all: a worker that spent a
   whole cap without touching the tree will do it again, and the brief is what
   needs looking at.
5. **For NEEDS MERGE - the branch, and what is on it.** The `note:` says which
   of the two cases it is (the step's own work would not replay, or work an
   earlier run stranded would not). Name the scratch branch -
   `overnight/<run name>/<step id>` - and put the commits in front of the user
   with `git log --oneline <your branch>..<the scratch branch>`. Say plainly
   that the work **passed its gates** where it was built and that nothing has
   been lost or discarded. If the step's directory has a `stranded.log`, that
   is the gate output for work an earlier run left behind; quote its verdict.
6. **For REVIEW REWORK FAILED - the review's findings, and what the rework
   attempt did about them.** Both are recoverable: `verdict.json` in the
   review step's directory has the findings the reviewer graded, and the
   rework attempt's own log (in the reviewed step's directory) shows what it
   tried and which gate stopped it. Say plainly that the reviewed commit
   **still stands** - nothing was reverted - and that this is a judgement
   call, not a broken build: a person decides whether the findings matter
   enough to block on.
7. **Anything that step wrote to `overnight/DECISIONS-PENDING.md`.** Workers are
   told to write findings there as they learn them, so a step that got into
   trouble has usually said why.
8. **For BARREN - the command, and what it printed.** Both are in the `note:`:
   the exact argv of the last worker that produced nothing, and the first 400
   characters it wrote. Put them in front of the user and say which key you
   think is wrong - compare the `--model` and `--effort` on that line against
   the step's `model:`, `effort:` and `budget_usd:` in the plan, and against
   `run.defaults`. This is usually a one-character fix and the user should not
   have to go and find it.
9. **What else was in flight**: how many steps remain, and whether any of them
   depend on this one. A stuck step that nothing depends on is a different
   conversation from one that four steps build on.

## The options, as commands the user runs

Give these as commands, not as offers to act. The user resolves this, not you.

    # retry just that step, after fixing the ground or the brief
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --only <id>

    # accept it and carry on with the rest, deliberately
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --from <next id>

There is a fourth option that is not a command: **re-planning the step**, which
is right when the step is blocked because it was cut too big rather than because
the ground is wrong. Say it is available and say nothing more - do not recommend
it and do not offer to do it. If the user asks for it, `references/planning.md`
covers re-cutting an existing plan, and the new steps take new ids so the stale
outcome is left behind with the step it belonged to.

    # forget every recorded outcome - this launches nothing, it only forgets
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --reset-state

    # ...and then, as a separate command, start the plan again
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml

For HALTED, add: sort the branch out first - `git log`, decide what to keep, and
either keep the foreign commit or move it aside - because until the branch is one
person's again the next reset will refuse in the same way.

**For NEEDS MERGE the commands above are the wrong ones.** The step is recorded
as complete: it is blocking, but it is deliberately not re-run on a resume,
because the work exists and re-running would do it a second time. So the user
lands the branch by hand and then relaunches normally, which picks up at the
next step. Give these, with the real branch name substituted:

    # what is on the branch that your tree does not have
    git log --oneline <your branch>..overnight/<run name>/<step id>
    git diff <your branch>...overnight/<run name>/<step id>

    # land it - a merge keeps the history, a rebase replays it, and the conflict
    # the runner hit is the one you will resolve either way
    git merge overnight/<run name>/<step id>

    # or, having decided it is not wanted, drop it
    git branch -D overnight/<run name>/<step id>

    # then carry on from where the run stopped
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml

Run the step's gates yourself after landing it. They passed on the scratch
branch, against a tree that is not quite this one, and the merge is exactly the
event that can break that. If the plan has a `kind: gate` checkpoint, its command
is the one to run.

**For REVIEW REWORK FAILED the commands above do nothing by default** - the
reviewed commit is recorded complete, so a plain relaunch skips both the step
and its review and carries on from the next one. That is the right move if
the user accepts the reviewed commit as it stands:

    # accept the reviewed commit and carry on with the rest
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --from <next id>

If instead the findings matter and the user wants another rework attempt -
typically after editing the brief, or fixing the code by hand and committing
it themselves - force the review to run again:

    # re-review the same commit (or one the user amended by hand)
    python <skill dir>/overnight.py --spec <abs>/overnight/steps.yaml --only <review id> --rerun

Without `--rerun` this does nothing: the review step is recorded complete and
`--only` alone does not override that.

**For BARREN the fix is in the plan, not in the tree.** Correct the step's
`model`, `effort` or `budget_usd` (or `run.defaults`), then re-run that step
alone - it is resumable, so the ordinary `--only <id>` command above is the right
one. Before spending a night on it, check the command by hand: paste the argv
from the note with `-p "reply ok"` in place of the brief and see whether the CLI
accepts it. If it does, the cause was environmental after all - a model withdrawn
for a while - and the step needs nothing but a relaunch.

Editing the brief and retrying, or cutting the step from the plan by hand, are
both fine and both the user's call. Offer your reading of which is right, in one
line, with the reason - then let them choose.

## What blocking costs, and why it is still right

One STUCK step blocks the whole plan, including steps that do not depend on it
and would have run perfectly well. Say so if it applies, and name the `--from`
command that overrides it. The trade is deliberate: a stuck step is usually a
signal that the plan was wrong, and building four more steps on a wrong plan
overnight is the expensive outcome, not the lost hours.
