# CLAUDE.md - CCskill-overnight

## What this project is

The `overnight` Claude Code skill: an unattended runner for headless Claude Code
workers, driven by a steps specification, with git as the undo. Extracted from a
one-off script written for another project and generalised. Installed as a skill
available to every project, and published publicly under GPL-3.0.

`README.md` is the user-facing manual. Read it first.

## First thing, in any fresh checkout

    python install.py --check     # what is installed, and whether it points here
    python install.py             # link this checkout in as the skill

`install.py` does the whole install in one step: it links this directory to
`<scope>/.claude/skills/overnight/`, which is where Claude Code looks and is not
configurable - a directory junction on Windows (no administrator rights needed),
a symlink elsewhere. Real files stay here under version control; the harness
keeps reading the path it expects.

`--project <dir>` installs into a project rather than the user profile,
`--force` replaces an existing install, `--uninstall` removes the link without
touching the checkout, and `--copy` is a last resort for a filesystem that
cannot link - a copy DRIFTS, and a fix made here never reaches the skill that is
actually loaded.

**Never hand-edit the installed copy.** If `--check` reports "a real directory",
somebody did, or an older install left one behind: `--force` fixes it.

## Where things are

| file | what |
|---|---|
| `overnight.py` | the runner - the whole orchestrator, one file |
| `selftest.py` | drives every path against `fake_worker.py` in under a minute |
| `fake_worker.py` | a scripted stand-in for `claude -p`, for the self-test |
| `install.py` | link this checkout in as the skill |
| `SKILL.md` | the skill definition Claude Code reads: a short router over four modes |
| `references/` | the procedures the router loads - one per mode, plus the spec format |
| `examples/` | a complete fictional run package; `try_it.py` runs it against the fake worker |
| `docs/ROADMAP.md` | what is not built yet, and why each matters |
| `LICENSE` | GPL-3.0 |

## The two stages

The skill has two separable stages, and the separation is the point:

- **PLAN** (`references/planning.md`) reads a project, interviews the user, and
  writes a spec and briefs into **that project's** `overnight/`.
- **RUN** (`references/launching.md`) launches it later, reading its inputs off
  disk rather than out of a conversation, so a `/clear` or a night's sleep
  between the two costs nothing.

Plus **PROGRESS** (`references/progress.md`) over a run's own files, and HELP.

## Hard rules

1. **The self-test must pass before any launch.** `python selftest.py` prints
   `SELFTEST PASS` or the run does not start.
2. **A fixture must match the production convention.** The self-test used
   `review-s1` where every real spec uses `review:s1`, and so missed a crash that
   killed a live run at its first review step. Fixture ids, gate shapes and paths
   are copied from a real spec, never invented.
3. **No project-specific reference in the runner, the skill or the references.**
   Gates are shell commands and briefs are file paths; the runner knows nothing
   about any particular repository. Worked examples live in `examples/`, are
   fictional, and say so on their first line.
4. **Nothing confidential, ever.** This repository is public. No client name, no
   deal figure, no private path beyond the author's own machine in an example.
5. **Nothing instanced is written here.** A project's spec, briefs, run output
   and decisions belong in that project's `overnight/`. This repository is the
   canonical code and its examples, shared by every project that uses the skill.
6. **ASCII only, no heredocs, write the file and run the file.**
7. **Every source file carries the GPL notice.** New `.py` files get the same
   fourteen-line header as the others, above the module docstring - not inside
   it, because several modules pass `__doc__` to argparse.

## Working conventions

- Commit after each working increment.
- Every change to the runner is covered by the self-test, or says why not. A
  guard for a defect must be shown to FAIL against the code before the fix.
- Changes that alter a running contract (step ids, gate forms, the state file,
  the `overnight/` layout) are noted in `README.md` as well as the code.
- After changing `SKILL.md` or anything in `references/`, remember the harness
  reads the skill list at session start: a new session may be needed to see it.

## Open items

See `docs/ROADMAP.md`.
