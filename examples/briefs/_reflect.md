# reflect-1 - look at what the run has learned, and change the plan if it should

*EXAMPLE. A reflect brief is optional; the runner has its own instructions. This
file is for the guidance that is specific to THIS project.*

You may edit `overnight/steps.yaml`, and only the steps that have not started.
The runner validates your rewrite and reverts it entirely if it touches a
completed step, reorders one, changes the `run:` block, names a brief that does
not exist, or produces invalid YAML.

## What is worth changing

- **A step whose ground has moved.** If an earlier step found that the callers
  rely on `None` meaning "no limit", the `cli` step as written is wrong. Rewrite
  its brief, or cut it and add a step that resolves the semantics first.
- **A step that is now bigger than one deliverable.** Split it. Two fifteen-
  minute steps beat one forty-minute step that gets reset.
- **A step whose dependency is STUCK.** Cut it rather than let it fail on ground
  it cannot fix. Say so in your rationale; the morning can re-add it.

## What is not worth changing

- Do not add steps because there is time left. An unplanned step at 02:00 has
  had none of the reading that the planned ones had.
- Do not lower a gate. If a gate is wrong, say so in the decisions file and cut
  the step; do not weaken the contract so it passes.
- Do not rewrite a brief to match what a worker happened to build.

## Your rationale

Whatever you change, the rationale becomes the commit message for the plan
change. Write it for somebody reading the history in a week: what you learned,
what you changed because of it, and what you deliberately left alone.
