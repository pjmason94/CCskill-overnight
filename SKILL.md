---
name: overnight
description: Plan, launch and report an unattended overnight run of headless Claude Code workers - build steps with gates and retries, read-only review steps with typed verdicts, reflect steps that may rewrite the remaining plan, and a git undo under all of it. Two separable stages - PLAN writes a steps spec into the current project, RUN launches it later - and the mode is inferred from the plan file, not asked for. Trigger on "/overnight", "/overnight plan", "/overnight run", "/overnight progress", "/overnight help", "set up an overnight run", "run this overnight", "prepare tonight's run", "how is the overnight run going".
---

# /overnight

An unattended runner for headless Claude Code workers. The orchestrator is a
Python script and not a model, so its context does not climb over a night. It
executes a **steps spec** as a sequence of `claude -p` workers, each given one
deliverable, gated by commands, sandboxed by git, retried with a diagnostic,
reviewed by a read-only worker, and re-planned by a reflect worker that may
rewrite the steps still to come.

`README.md` beside this file is the full manual. Read it when a question here is
not answered.

## First, ask the project which mode this is

Do not reason about it. Run this from the project root:

    python <skill dir>/overnight.py --mode

It prints one word on the first line, then the reason and the relevant step ids:

| it prints | what it means | do this |
|---|---|---|
| **BLOCKED** | a step is STUCK, HALTED or OVER BUDGET | **stop.** Load `references/blocked.md` |
| **PLAN** | there is no plan file | load `references/planning.md` |
| **RUN** | the plan has steps still to run | load `references/launching.md` |
| **REPLACE?** | every step completed | ask whether to replace the plan; if yes, `references/planning.md` |

`--mode` exits 3 on BLOCKED and 0 otherwise, and it reads the plan file alone -
`overnight/steps.yaml`, in which every outcome is recorded as a `done:` block.

**BLOCKED wins over everything.** It is checked before the others and it is a
hard stop, not a menu: a STUCK step has already had every retry the runner has,
a HALTED step means somebody else committed to the branch mid-step, an OVER
BUDGET step wants a smaller brief or a bigger cap, and none of the three is a
thing this skill can resolve. Do not offer to resume, do not offer to
re-plan, do not proceed to another mode. Report it and hand it back.

Two modes are asked for directly rather than inferred, and skip `--mode`:

| the user says | load |
|---|---|
| `/overnight progress`, "how is the run going", "what happened overnight" | `references/progress.md` |
| `/overnight help`, "what does overnight do" | answer from this file; offer the manual |

If the user names a mode explicitly (`/overnight plan` when a plan already
exists), still run `--mode` first and say what it found before doing as they
asked - except for BLOCKED, which stops regardless.

**Load exactly one reference file, then follow it.** They are procedures, not
background reading, and each is written to be executed top to bottom.

## The two stages, and why they are separate

**PLAN** reads the project, interviews the user, and writes a spec and a set of
briefs into **the project you are working in** - never into the skill's own
directory. **RUN** launches that spec, hours or days later, reading its inputs
off disk rather than out of a conversation. A `/clear`, a crash or a night's
sleep between the two costs nothing, because the plan is the handoff.

Everything a particular project's run needs, and everything it writes, lives
under that project's `overnight/`:

    <project>/overnight/
      steps.yaml               the plan            - committed
      briefs/_preamble.md      shared worker rules - committed
      briefs/<step-id>.md      one per build step  - committed
      briefs/_reflect.md       optional guidance   - committed
      runs/<run-name>/         everything the run writes - GITIGNORED
      DECISIONS-PENDING.md     findings and questions   - GITIGNORED

The plan is committed because the runner refuses a dirty tree and a reflect step
commits its own rewrite. The output is ignored so a worker's commits never carry
the run's logs.

## Never

- **Never write a project's spec, briefs or run output into the skill directory.**
  That directory is the canonical code and its examples, shared by every project.
- **Never launch the run yourself.** The workers need `bypassPermissions` and the
  auto-mode classifier blocks that from inside a session, correctly. Hand the
  user a command to paste into their own shell.
- **Never launch without `python selftest.py` printing `SELFTEST PASS`.**
- **Never use `--bare`** (it bills the API account and disables CLAUDE.md
  discovery, so a worker would not load the project's rules).

## Install

The live copy Claude Code loads is `<scope>/.claude/skills/overnight/`, and that
path is not configurable. `python install.py` links the checkout there in one
step - a junction on Windows, a symlink elsewhere - so the repository stays the
source of truth. `--check` reports, `--force` replaces, `--uninstall` removes.

## Files

| file | what |
|---|---|
| `overnight.py` | the runner. Report and exit, launching nothing: `--list`, `--print-brief ID`, `--format`, `--progress`, `--mode`, `--reset-state`. `--dry-run` spawns no worker and writes no outcome, but it does execute each gate command. Launch for real: bare, or with `--from ID`, `--only A,B`, `--rerun` |
| `selftest.py` | every path, against `fake_worker.py`, in under a minute for no tokens |
| `install.py` | link this checkout in as the skill |
| `references/blocked.md` | a STUCK or HALTED step: the report, and the hand back |
| `references/planning.md` | stage 1: read, interview, write the spec and briefs |
| `references/launching.md` | stage 2: self-test, dry run, commit, launch, morning |
| `references/progress.md` | read a run in flight or a finished one |
| `references/spec-format.md` | the steps file, gate forms, step kinds, tiers |
| `examples/` | a complete fictional run package that works against the fake worker |
| `README.md` | the full manual |
| `docs/ROADMAP.md` | what is not built yet |
