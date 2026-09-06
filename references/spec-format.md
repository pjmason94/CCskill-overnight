# The steps file

`python overnight.py --format` prints a condensed version of this. Section 7 of
`README.md` is the exhaustive reference; this is what you need to write one.

By convention the file lives at `<project>/overnight/steps.yaml`.

```yaml
run:
  name: night-2                  # output goes to overnight/runs/night-2/
  hours: 7                       # stop STARTING steps after this many hours
  attempts: 3                    # build attempts before STUCK
  worker_timeout_min: 90         # default hard kill per worker
  budget_usd_per_step: 40        # optional; --max-budget-usd on each worker
  isolation: worktree            # THE DEFAULT; `in-place` is the opt-out. Each
                                 # build step gets its own worktree on a scratch
                                 # branch, integrated once its gates pass, so
                                 # nothing the operator does to their own tree
                                 # mid-step can fail a worker's gate.
  worktree_link: [.venv]         # paths git does not carry, linked into each
                                 # worktree; preflight refuses to start if the
                                 # universal gates pass in your tree and fail in
                                 # a fresh one, which is what a missing one looks
                                 # like
  preamble: overnight/briefs/_preamble.md    # {CHUNK} is replaced by the step id
  decisions_file: overnight/DECISIONS-PENDING.md
  out: overnight/runs            # the parent of the run directory
  defaults:                      # tier per kind
    build:      {model: opus,   effort: medium}
    review:     {model: opus,   effort: high}
    reflect:    {model: opus,   effort: high}
    diagnostic: {model: opus,   effort: high}
  gates:                         # universal; appended to EVERY build step
    - {name: full suite, cmd: python -m pytest -q}
    - {clean_tree: true}

steps:
  - id: 2a-parser
    kind: build
    title: one line saying what it delivers
    brief: overnight/briefs/2a-parser.md
    model: sonnet                # per-step tier override
    effort: medium
    expected_min: 15             # recorded beside the actual; terminates nothing
    timeout_min: 40              # hard kill; about 2.5x the estimate
    gates:                       # this step's own, run before the universal ones
      - {cmd: python -m pytest -q tests/test_parser.py::test_handles_empty_input}

  - id: review:2a-parser         # the convention: `review:<the step it reviews>`
    kind: review
    of: 2a-parser
    on_fail: rework              # record | rework | revert
    brief: overnight/briefs/_review-extra.md    # optional

  - id: reflect-1
    kind: reflect
    brief: overnight/briefs/_reflect.md         # optional guidance

  - id: measure
    kind: gate                   # commands only, no worker
    gates:
      - {file: out/measurement.json}
```

## Gate forms

| form | passes when |
|---|---|
| `{cmd: <shell command>}` | it exits 0 |
| `{cmd_empty: <shell command>}` | it exits 0 **and** prints nothing |
| `{file: <path>}` | the path exists and is not empty |
| `{fresh_shell: <command>}` | it exits 0 in a NEW shell with the machine PATH rebuilt - the only honest test of "works in a new terminal" |
| `{clean_tree: true}` | `git status --porcelain` is empty, ignoring untracked files that were already there when the step began |

Add `{name: ...}` to any gate to give it a readable label in the log.

A gate is a command that exits 0 or not, **never a judgement**. Where an exit
criterion is a claim about behaviour, name the test node id and let the gate run
it. The worker writes the test; the gate proves it.

## Step kinds

**build** - a worker at `bypassPermissions` with the brief on stdin. It commits
its own work. Up to `attempts` tries; a failed gate resets to the attempt's
baseline. After the second failure a **diagnostic** worker reads a compact
transcript of both attempts - the assistant's words and every tool error, tens of
KB instead of megabytes - and writes `remediation.md`, which the third attempt
ingests. Still failing: STUCK, and the run moves on.

**review** - reads a passed commit named by `of:`, at `bypassPermissions` with
Edit, Write and NotebookEdit disallowed, and returns a typed verdict through
`--json-schema`: `pass | rework | fail`, findings with severity, a `rework_brief`.
The tree is reset after it regardless.

- `on_fail: rework` runs ONE build attempt on top of the reviewed commit with the
  findings appended. If that fails its gates, the reviewed commit stands.
- `on_fail: revert` resets to before the reviewed commit.
- `on_fail: record` writes `verdict.json` and nothing else.

**reflect** - reads the run so far, the pending steps and the decisions file, and
may edit the spec: **pending steps only**. The runner re-reads the spec before
every step and, after a reflect, validates it - completed steps unchanged and in
order, `run:` unchanged, every new brief present, valid YAML - reverting from a
backup on any violation, and otherwise committing the plan change with the
reflect's rationale as the message.

**gate** - commands only, no worker. For a long measurement whose result is a
number in a file.

## The ledger: `done:`

**The steps file is the record of the run.** There is no `state.json`. After
every step the runner splices a `done:` mapping into that step's own block and
commits the file as `overnight: <step id> <outcome>`:

```yaml
  - id: parse-durations
    kind: build
    brief: overnight/briefs/parse-durations.md
    expected_min: 15
    gates:
      - {cmd: python -m pytest -q tests/test_parse_durations.py}
    done:
      outcome: "PASS"
      at: "2026-09-05 10:22"
      sha: "f756c08a"
      attempts: 1
      minutes: 7.7
      cost_usd: 2.39
      tier: "opus/medium"        # what the runner asked for, not what the
                                 # commit trailer says
      note: "committed at f756c08a"
```

Review steps also carry `of:` and `findings:`; a reflect that changed the plan
carries `added:` and `removed:`; a step reworked after a review carries
`reworked: true` with its `sha` updated. There is never more than one `done:` per
step - a rework replaces it in place.

The worker's long summary is NOT in the yaml. It goes to
`overnight/runs/<run>/<step>/summary.md`, so the plan stays scannable.

**You may edit a pending step by hand while a run is live.** The runner re-reads
the file immediately before each write and splices only the block it is
recording, so an edit to a step that has not started survives. Editing a step
that already carries `done:` is not defended against - do not.

**Comments survive.** The splice is textual and verified: the runner re-parses
the result and refuses any edit that would change another step. It never
re-dumps the file, because that would destroy comments, key order and quoting.

## Resume

A step is complete iff it carries `done:` with an outcome outside `STUCK`,
`HALTED`, `FAIL`, `INCONCLUSIVE`, `SKIPPED`, `REWORK FAILED` and
`REVERTED BY REVIEW`. A relaunch skips what completed and re-runs the rest.
`--rerun` forces everything; `--reset-state` strips every `done:` from the file,
commits that and exits without launching - the flag has outlived the file it was
named for.

`--mode` prints what to do next from the plan alone: `BLOCKED` (a STUCK or
HALTED step needs a person; exits 3), `PLAN` (no plan file), `RUN` (steps still
to run) or `REPLACE?` (everything completed).
