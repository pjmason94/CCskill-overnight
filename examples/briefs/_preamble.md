# Overnight run - the rules, for every worker

*EXAMPLE. The project described here does not exist. Copy the shape, not the
contents.*

You are one step of an unattended overnight run. Nobody is awake. `{CHUNK}` is
your step id; the rest of this file is the same for every step.

## What you are working on

`tickerbell` is a small library for turning durations into words. It has no
dependencies beyond the standard library and pytest. The layout is:

    src/tickerbell/          the library - importable, no side effects at import
    tests/                   pytest, one file per module
    docs/decisions.md        why things are the way they are

## How to finish

1. **Do the one thing your brief names.** If your brief seems to contain two
   deliverables, do the first properly and write the second to the decisions
   file. A step that does two things badly is worse than a step that does one.
2. **Write the test your brief names, by its exact node id.** Where the test
   guards a defect, show it FAILING against the defect before you fix it. A test
   that has not been shown to fail is not a guard.
3. **Commit your own work, by explicit path.** Never `git add -A`. The repository
   is not frozen while you work and a stray file from somewhere else swept into
   your commit will be judged by your gates.
4. **Leave the tree clean.** An uncommitted change fails the clean-tree gate and
   your whole step is reset.

## When you are stuck

Do not guess and do not widen your scope to route around a problem. Write what
you found to `overnight/DECISIONS-PENDING.md` **as you learn it, not at the end**
- a failed gate resets the tree and destroys code, but that file is ignored by
git and survives. One entry per finding:

    ## {CHUNK}: <the question in one line>
    What I found: ...
    What I would do: ...
    Why I did not: ...

Then finish what you can and stop. A step that stops does not abort the run.

## What never to do

- Never push, never touch a remote, never rewrite history.
- Never edit `overnight/steps.yaml` or another step's brief.
- Never disable, skip or weaken a test to make a gate pass.
- Never install a dependency that is not already in the project.
- Never touch a module your brief does not name.

## Your summary

End with what you built, what you deliberately left alone and why, and anything
the morning should look at first. The best summaries are the ones that say what
was NOT done.
