# Design: each build worker in its own git worktree

Roadmap item 2. Written 2026-09-06, before any of the code. The interim lock is
already built; this is the part that removes the conflict rather than detecting
it.

## What changes, in one line

A build step's worker runs in a private worktree on a scratch branch. The
operator's branch and working tree are never written to by a worker, never reset,
and never cleaned. Integration onto the operator's branch happens once, after the
gates pass, under the runner's control.

## What runs where

| kind | where | why |
|---|---|---|
| build (and its rework, and its diagnostic) | a worktree | it writes code; it is the only thing that does |
| review | the main repo | read-only over a committed sha; it needs the sha to exist on the branch |
| reflect | the main repo | it edits `steps.yaml`, which is the ledger and lives here |
| gate | the main repo | it asks about the project as it now stands |
| preflight | both | see "the gate that must run twice" |

Only the build path moves. That keeps the change to one method and its retry
loop, and leaves the ledger, the review and the reflect exactly as they are.

## The worktree must live OUTSIDE the repository

Not under `overnight/runs/`, gitignored or not. A universal gate like
`pytest -q` walks the tree from the project root and does not read `.gitignore`:
a worktree inside the repo means every test file is collected twice, under one
module name, and the universal gate fails for a reason that has nothing to do
with the worker. This is the same defect the `.quarantined` suffix exists to
avoid, and it would be reintroduced at a hundred times the scale.

    <repo>/../<repo name>.overnight-worktrees/<run>/<step>/

Configurable as `run.worktree_root`. Adjacent to the repository so it is easy to
find and easy to delete; outside it so nothing collects it.

## The killer problem: gitignored dependencies

A fresh worktree has no `.venv`, no `node_modules`, no `.env`, no build cache.
Gates that pass in the main tree fail there - and they fail for reasons that are
nothing to do with the worker, which the runner would record as the worker's
failure, burn three attempts on, and report as STUCK in the morning.

Three defences, in order of importance:

1. **The gate that must run twice.** At preflight, when isolation is on, run the
   universal gates in a throwaway worktree as well as in the main tree. If they
   pass in the main tree and fail in the worktree, REFUSE TO START and say why in
   those words - the gates depend on something that is not in git. This is the
   whole night's risk bought for one worktree and one gate run, and it is the
   single most valuable part of this design. Do not make it optional.
2. **`run.worktree_link: [".venv", "node_modules", ...]`** - paths linked into
   each worktree from the main repo. Directory junctions on Windows (no admin,
   the mechanism `install.py` already uses), symlinks elsewhere. Link, never
   copy: a copied `.venv` is stale the moment anything installs.
3. **`run.isolation: worktree | in-place`**, defaulting to `in-place`. See below.

## The default stays `in-place` at first, deliberately

A change that makes every gate in every project run in a different directory
cannot become the default on the day it is written. Ship it opt-in; the
preflight double-gate is what tells a project whether it can turn it on. Flip the
default once two real projects have run a night on it - and say so in the README
rather than leaving the default as an accident.

## Integration, and the one new outcome

The worker commits on `overnight/<run>/<step>`, based at the step's baseline.
After the gates pass in the worktree:

- **branch has not moved** (`HEAD == base`): `git merge --ff-only`. The common
  case, and free.
- **branch has moved** (the operator committed, or an earlier step did):
  `git rebase --onto HEAD base <scratch>` then fast-forward. Replaying is the
  point of the whole design - today this is where a foreign commit gets the step
  HALTED.
- **it conflicts**: abort cleanly, keep the worktree, keep the branch, record
  **`NEEDS MERGE`** with the branch name in the note.

`NEEDS MERGE` is strictly better than today's HALTED: the work exists, on a named
branch, in the ledger and in the morning report. Today it is discarded and
recoverable only from a rescue tag.

**A `NEEDS MERGE` stops the run** - unlike HALTED, which lets the run continue.
The difference is real and worth stating in the code: HALTED means the step's
work was discarded, so the tree the remaining steps build on is still the tree the
plan assumed. NEEDS MERGE means work exists that the tree does not have, so every
later step would build on a tree the plan did not intend. Add it to
`BLOCKING_OUTCOMES` so the next `--mode` returns BLOCKED.

The sha recorded in the ledger is the sha ON THE OPERATOR'S BRANCH after
integration, not the worktree's commit - a rebase changes it, and the ledger's sha
is what the reviewer reads and what the morning inspects.

## Lifecycle

- **One worktree per STEP**, not per attempt. Between attempts, reset it in place
  - that is what `safe_reset` already does, and it now resets a tree nobody else
  is touching, so the quarantine cannot be triggered by a stranger's file.
- **Removed at step end**, unless the outcome is `NEEDS MERGE`.
- **Orphans after a crash**: at preflight, `git worktree prune`, then remove any
  directory under `worktree_root` that git no longer knows about. **Never delete a
  scratch branch that is not merged into HEAD** - that is somebody's work, and
  deleting it silently is precisely the class this whole item exists to close.

## What the worker sees

`OVERNIGHT_REPO` becomes the worktree; add `OVERNIGHT_MAIN_REPO` for the real one.
Gate commands run with `cwd` = the worktree. Briefs are read by the runner from
the main repo and their text is unchanged - the convention is already
repo-relative paths, so this costs nothing, but preflight should warn if a brief
contains the main repo's absolute path, because that path will silently point
outside the worker's tree.

`has_git` false means no worktrees: fall back to in-place with one log line.

## Self-test coverage this needs

The interesting ones are the last three; the first two are table stakes.

1. A build step runs in a worktree, its commit lands on the main branch, and the
   main working tree is never dirty during the step.
2. `isolation: in-place` behaves exactly as today - every existing case must pass
   unchanged, which is the guard that this is additive.
3. **A third party commits to the branch mid-step and the step still passes.**
   This is the demonstration: the existing `foreign` fixture HALTs today. Same
   fixture, isolation on, and it integrates. Nothing is discarded, nothing is
   refused, no rescue tag is needed.
4. **A third party's commit that genuinely conflicts** -> `NEEDS MERGE`, the
   scratch branch still holds the work, `git log <branch>` proves it, the run
   stops, and the morning names the branch.
5. **A gate that needs a gitignored path** fails at PREFLIGHT with the words
   about git, not at 03:00 as a STUCK step.
6. Orphan cleanup: a leftover worktree directory is pruned; an unmerged scratch
   branch is NOT deleted.

## Cost

Real but bounded: worktree lifecycle, the preflight double-gate, the rebase and
its conflict path, one new outcome and its place in `BLOCKING_OUTCOMES`, the
`cwd` threading through `run_gates` and `worker_argv`, and six self-test cases.
Disk is one checkout at a time, because steps are sequential.
