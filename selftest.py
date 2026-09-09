# overnight - unattended runs of headless Claude Code workers
# Copyright (C) 2026 Paul Mason
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
"""selftest.py - exercise every path of overnight.py against a fake worker, in a
throwaway git repository, for no tokens.

    python selftest.py                the whole suite, and the ONLY form that
                                      prints SELFTEST PASS
    python selftest.py --list         the sections, what each needs, and how many
                                      checks each makes
    python selftest.py --only 13,17   just those, plus anything they need
    python selftest.py --from 17      from there to the end of the suite
    python selftest.py --quiet        the whole suite without the progress line

The full suite prints a progress line about once a minute - percent complete,
checks done against the total, elapsed, and an ETA from the mean time per check
so far. Run it unbuffered into a FILE and read the file's tail on demand:

    python -u selftest.py > run.log 2>&1
    Get-Content run.log -Tail 5       (PowerShell; add -Wait to follow)

Never pipe it through `tail`, `grep` or `sort`: the pipe holds every byte until
the process exits, and a perfectly streamed heartbeat produces an empty log.

IT TAKES MINUTES, NOT SECONDS - a QUARTER OF AN HOUR, against a docstring that
claimed "under a minute" until 2026-09-07. Two runs that day measured 12 min
42 s over 235 checks and 18.7 min over 249; the whole suite is real
subprocesses doing real git work, and it grows with every section added. Do not
trust this paragraph - every run ends with a table of where its own time went.
While working on one change, run the sections that cover it; run the whole suite
before you commit, and before any launch. A partial run says SELFTEST PARTIAL OK
and never the token a launch is gated on.

Paths covered: a build that passes; a build that fails twice (once by leaving no
test, once by leaving the tree dirty), gets a diagnostic, and passes on the third
attempt with the remediation ingested; a review that returns `rework` and the
rework that follows; a reflect that adds a step, which then runs; a reflect that
tampers with a completed step and is reverted; the clean-tree preflight refusal; a
rescue tag over work a gate discarded; a project with NO GIT AT ALL, which must
degrade (warn once, skip clean_tree, skip review) rather than refuse; a THIRD
PARTY committing to the branch mid-step, which must be refused and never
destroyed; the LEDGER - `done:` spliced into the plan file without disturbing a
comment or another step - and `--reset-state` stripping it; `--mode`, which must
say BLOCKED for a stuck plan and never anything else; the USAGE WALL, where the
workers stop answering entirely and the run must park (or stop) rather than march
through the plan marking untested steps STUCK; and the WORKTREES AN EARLIER RUN
LEFT BEHIND - a registered one that must be handed to git rather than torn off
the filesystem, a dangling registration that must not stall the step, a genuine
orphan that must still go, and a crashed run's commits on a scratch branch, which
are tagged before `worktree add -B` moves the branch off them.

Run this before every overnight launch. A runner path that has not been exercised
is a path that will be exercised for the first time at 03:00.
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "overnight.py"
FAKE = HERE / "fake_worker.py"

sys.path.insert(0, str(HERE))
from overnight import (Log, SpecError, _step_blocks, is_resumable,   # noqa: E402
                       parse_until, upsert_done)


def _blocks_of(text):
    lines = text.splitlines(keepends=True)
    return {sid: "".join(lines[a:b]) for sid, a, b, _ in _step_blocks(text)}

SPEC = """\
run:
  name: selftest
  hours: 1
  attempts: 3
  # Comfortably above the largest expected_min below (15) - A2 refuses a plan
  # whose timeout cannot outlast its own estimate, and this bound never
  # actually elapses against the fake worker regardless of its size.
  worker_timeout_min: 20
  # PINNED, not inherited. Isolation is the default now, and every in-place
  # defence below - the reset between attempts, the quarantine, the rescue tag,
  # the refusal over a foreign commit - is a defence of the tree the operator
  # shares. Letting this fixture take the default would leave all of it untested.
  isolation: in-place
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q}
    - {clean_tree: true}
steps:
  # A COMMENT THE LEDGER MUST NOT DESTROY. The runner splices `done:` into this
  # file after every step; a re-dump would silently take this line with it, which
  # is exactly the failure the splice exists to avoid.
  - id: s1
    kind: build
    title: passes first time
    brief: overnight/briefs/s1.md
    expected_min: 15
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: review:s1
    kind: review
    of: s1
    on_fail: rework
  - id: s2
    expected_min: 12
    kind: build
    title: fails twice then passes
    brief: overnight/briefs/s2.md
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
  - id: reflect-1
    kind: reflect
  - id: s3
    expected_min: 10
    kind: build
    title: passes
    brief: overnight/briefs/s3.md
    gates:
      - cmd: python -m pytest -q tests/test_s3.py
  - id: reflect-2
    kind: reflect
"""

# One build step, one attempt, for the third-party-commit case. `attempts: 1` keeps
# the case about the reset and not about the retry loop.
# The same one-step case, isolated. `attempts: 1` keeps it about the isolation.
ISOLATED_SPEC = """\
run:
  name: isolated
  hours: 1
  attempts: 1
  worker_timeout_min: 20
  isolation: worktree
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: a stranger commits to the branch while this step runs
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
"""

# NO `isolation:` key - the point of the case is what the DEFAULT does. Two
# attempts, so that "it fails, and then it fails again the same way" is visible.
DEFAULT_ISO_SPEC = """\
run:
  name: default-iso
  hours: 1
  attempts: 2
  worker_timeout_min: 20
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: the operator writes a file into their own tree mid-step
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
"""

# THE PER-STEP BUDGET. Three attempts on purpose: the defect being guarded is
# that a cap trip used to be retried, so a $6 cap billed $18 to be cut off in the
# same place three times. The cap here is the $6 the fake worker reports spending.
BUDGET_SPEC = """\
run:
  name: budget
  hours: 1
  attempts: 3
  worker_timeout_min: 20
  budget_usd_per_step: 6
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: the brief asks for more than the cap will pay for
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
"""

# CONTINUATION. The run cap is 6 and the STEP's own is 2, so the same fixture
# proves both that a per-step cap overrides the run's and that a cut-off worker is
# handed on rather than repeated. `continuations: 1` - one extra worker, so the
# bound is testable in two legs rather than four.
CONTINUE_SPEC = """\
run:
  name: continue
  hours: 1
  attempts: 3
  worker_timeout_min: 20
  budget_usd_per_step: 6
  continuations: 1
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: a step bigger than one worker's budget
    brief: overnight/briefs/s1.md
    budget_usd: 2
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: review:s1
    kind: review
    of: s1
    on_fail: rework
"""

FOREIGN_SPEC = """\
run:
  name: foreign
  hours: 1
  attempts: 1
  worker_timeout_min: 20
  isolation: in-place              # the refusal being tested is an in-place defence
  preamble: overnight/briefs/_preamble.md
  gates:
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: a stranger commits while this step runs
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
"""

# THE USAGE WALL. Two steps, so that "s2 was never started" is a thing the run
# can be asked about; three attempts, so a whole step's worth of retries is spent
# on a worker that returns nothing, which is exactly the shape of the incident
# (2026-09-07) rather than a contrived single failure.
WALL_SPEC = """\
run:
  name: wall
  hours: 1
  attempts: 3
  worker_timeout_min: 20
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 15
    kind: build
    title: the account's window closes during this step
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: s2
    expected_min: 12
    kind: build
    title: must never be started while the wall stands
    brief: overnight/briefs/s2.md
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
"""

# THE BREAKER'S BLIND SPOT. Same shape as WALL_SPEC - one step that goes barren
# and one that must still get its turn - but `expected_min: 1` so the whole plan
# fits in a two-minute clock, which is what bounds the demonstration of the OLD
# behaviour: without the fix this run parks, re-runs s1, parks again, and does
# that until the stop time rather than for the hour WALL_SPEC allows.
BARREN_SPEC = """\
run:
  name: wall
  hours: 1
  attempts: 3
  worker_timeout_min: 2
  park_poll_min: 0.02
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 1
    kind: build
    title: its own tier is wrong, so its workers never start
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: s2
    expected_min: 1
    kind: build
    title: must still get its turn
    brief: overnight/briefs/s2.md
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
"""

# A reflect whose worker never answers. One build step first so the reflect has
# something to reflect over, and the wall threshold is never reached - the point
# is the SINGLE barren reflect, not a cascade.
BARREN_REFLECT_SPEC = """\
run:
  name: wall
  hours: 1
  attempts: 1
  worker_timeout_min: 2
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    expected_min: 1
    kind: build
    title: passes normally
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: reflect-1
    kind: reflect
"""

SCENARIO = {
    "s1": ["pass"],
    # `+stray`: the reviewer leaves an untracked file behind. The reset that
    # follows the most read-only step in the run must quarantine it, not delete it.
    "review:s1": ["review:rework+stray"],
    "s1#rework": ["pass"],
    "s2": ["commit-wrong", "fail-dirty", "pass"],
    "s2#diag": ["diag"],
    "reflect-1": ["reflect:add"],
    "s3": ["pass"],
    "added-by-reflect-1": ["pass"],
    "reflect-2": ["reflect:tamper"],
}


def sh(repo, *argv, check=True):
    return subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=check,
                          encoding="utf-8", errors="replace")


def spec_text(repo):
    return (Path(repo) / "overnight" / "steps.yaml").read_text(encoding="utf-8")


def ledger(repo):
    """{step id: its done: mapping} straight out of the plan file.

    The plan file IS the record of the run; this is how every assertion below
    reads an outcome, and it is deliberately the same route the runner takes.
    """
    spec = yaml.safe_load(spec_text(repo))
    return {s["id"]: s["done"] for s in spec["steps"] if s.get("done")}


def step_blocks_text(repo):
    """{step id: the raw text of its block}, for proving a splice touched one step."""
    text = spec_text(repo)
    lines = text.splitlines(keepends=True)
    return {sid: "".join(lines[a:b]) for sid, a, b, _ in _step_blocks(text)}


def scaffold(where):
    """The project layout an overnight run expects, without any git."""
    where.mkdir(parents=True, exist_ok=True)
    (where / ".gitignore").write_text("overnight/runs/\n__pycache__/\n.pytest_cache/\n",
                                      encoding="utf-8")
    (where / "tests").mkdir()
    (where / "tests" / "test_base.py").write_text("def test_base():\n    assert 1 == 1\n",
                                                  encoding="utf-8")
    briefs = where / "overnight" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "_preamble.md").write_text("# preamble for {CHUNK}\n", encoding="utf-8")
    for sid in ("s1", "s2", "s3"):
        (briefs / f"{sid}.md").write_text(f"Build {sid}.\n", encoding="utf-8")
    (where / "overnight" / "steps.yaml").write_text(SPEC, encoding="utf-8")
    return where


def make_repo(root):
    repo = scaffold(root / "repo")
    sh(repo, "git", "init", "-q")
    sh(repo, "git", "config", "user.email", "selftest@example.invalid")
    sh(repo, "git", "config", "user.name", "selftest")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "base")
    return repo


def make_plain_dir(root):
    """The same project, with no repository anywhere above it.

    `root` is a fresh temp directory, so nothing above this path is a repo either -
    which is the condition being tested, and would be silently untested if the
    scratch directory happened to sit inside one.
    """
    return scaffold(root / "plain")


def run_runner(repo, scenario_path, *extra):
    env = dict(os.environ)
    env["OVERNIGHT_FAKE_SCENARIO"] = str(scenario_path)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-u", str(RUNNER), "--spec", "overnight/steps.yaml",
         "--fake-worker", str(FAKE), *extra],
        cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")


def mode(where):
    """`--mode` over a plan, as the /overnight router reads it: (verdict, exit code).

    Module level because four sections ask it - 7 for its own sake, and 9, 10 and
    13 to prove that what they did to a plan changed which way the router sends it.
    """
    done = subprocess.run([sys.executable, "-u", str(RUNNER), "--mode"],
                          cwd=where, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return done.stdout.splitlines()[0] if done.stdout else "", done.returncode


class Ctx:
    """What a section is handed: the scratch root, the running tally, and the few
    fixtures one section builds for a later one to read.

    `shared` is deliberately a dict rather than attributes. A section that reads
    `c.shared["repo"]` without section 1 having run raises KeyError immediately,
    which is what the dependency table below exists to prevent - and a loud crash
    beats a section quietly testing a repository in the wrong state.
    """

    def __init__(self, root):
        self.root = root
        self.failures = []
        self.checks = 0
        self.shared = {}
        self.scenario_path = root / "scenario.json"
        self.scenario_path.write_text(json.dumps(SCENARIO), encoding="utf-8")
        # The heartbeat. `total` is the denominator and doubles as the switch:
        # None means no heartbeat, which is what a partial run and --quiet get.
        self.total = None
        self.started = time.time()
        self.section = ""
        self._stop = threading.Event()
        self._thread = None

    def check(self, name, condition, detail=""):
        self.checks += 1
        print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  ({detail})" if detail and not condition else ""))
        if not condition:
            self.failures.append(name)

    def start_heartbeat(self, total, every=60.0):
        """One progress line a minute, on the FULL suite only.

        The suite is 7 to 19 minutes and, until this existed, printed nothing an
        operator could plan around: "how long" could only be answered with a
        range too wide to be useful. The percentage is what makes the answer a
        number. The denominator is TOTAL_CHECKS, known before the run starts, and
        the ETA extrapolates from the mean time per check SO FAR rather than from
        any fixed figure - the spread on this machine is load, not work, so a
        fixed estimate is wrong on exactly the runs where it matters.

        ON A TIMER THREAD, not from check(): the suite's minutes are spent inside
        runner subprocesses, BETWEEN checks, and a heartbeat driven by the checks
        went five minutes without a line the first time it was tried. A daemon
        thread beats on the clock whatever the main thread is waiting on.

        Not on a partial run and not under --quiet: those outputs stay
        byte-identical to what they printed before, so two can be diffed. Written
        as one flushed write, because the point is to be readable from a log file
        while the suite is still running (redirect to a FILE and tail the file -
        never pipe through `tail`, which buffers every byte until the process
        exits).
        """
        self.total = total
        self._thread = threading.Thread(target=self._heartbeat, args=(every,), daemon=True)
        self._thread.start()

    def stop_heartbeat(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _heartbeat(self, every):
        while not self._stop.wait(every):
            self.beat()

    def beat(self):
        if not self.total:
            return
        elapsed = time.time() - self.started
        done = self.checks
        eta = (f"~{elapsed / done * (self.total - done) / 60:.1f} min left" if done
               else "ETA after the first check")
        sys.stdout.write(f"[{100 * done // self.total:3d}%]  {done}/{self.total} checks"
                         + (f" | section {self.section}" if self.section else "")
                         + f" | {elapsed / 60:.1f} min elapsed | {eta}\n")
        sys.stdout.flush()


def section_1_3(c):
    root = c.root
    check = c.check
    failures = c.failures
    scenario_path = c.scenario_path

    repo = make_repo(root)
    print("1. preflight refuses a dirty tree")
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    done = run_runner(repo, scenario_path)
    check("exit 2 on dirty tree", done.returncode == 2, done.stdout[-300:])
    check("says REFUSING", "REFUSING TO START" in done.stdout)
    (repo / "dirty.txt").unlink()

    print("2. the full scenario")
    done = run_runner(repo, scenario_path)
    out = repo / "overnight" / "runs" / "selftest"
    log = (out / "run.log").read_text(encoding="utf-8") if (out / "run.log").exists() else ""
    # The SPEC is the ledger. There is no state.json to read.
    steps = ledger(repo)

    def outcome(sid):
        return steps.get(sid, {}).get("outcome", "(absent)")

    check("s1 PASS", outcome("s1") == "PASS", outcome("s1"))
    check("s1 reworked after review", steps.get("s1", {}).get("reworked") is True)
    check("review:s1 REVIEW REWORK PASS", outcome("review:s1") == "REVIEW REWORK PASS",
          outcome("review:s1"))
    # The id carries a colon, as the convention and every real spec do; the
    # DIRECTORY is the sanitised form. Asserting the sanitised path is the
    # guard for the crash of 2026-09-05.
    check("verdict.json written", (out / "review-s1" / "verdict.json").exists())
    # The reviewer left an untracked file. The reset that tidies up after it
    # used to `git clean -fd` it away, silently and unrecoverably - the one
    # reset in the runner with no quarantine, after the one step documented as
    # read-only. It is kept now, under the step that swept it up.
    rescued = out / "review-s1" / "quarantine" / "review-s1-after-review"
    check("a stray left by the reviewer is quarantined, not deleted",
          (rescued / "review-scratch.txt.quarantined").exists(),
          str(sorted(p.name for p in rescued.glob("*")) if rescued.exists()
              else f"no {rescued}"))
    check("...and it is gone from the working tree",
          not (repo / "review-scratch.txt").exists())
    check("s2 PASS on attempt 3", outcome("s2") == "PASS" and steps["s2"].get("attempts") == 3,
          f"{outcome('s2')} attempts={steps.get('s2', {}).get('attempts')}")
    check("s2 remediation written", (out / "s2" / "remediation.md").exists())
    check("s2 attempt 3 ingested remediation", "attempt 3 ingests the remediation plan" in log)
    check("s2 attempt 1 failed on its own gate", "FAIL attempt-1 - failed the gate: cmd: python -m pytest -q tests/test_s2.py" in log)
    # NOT the gate's display name. `tree clean (the worker committed)` printed
    # after FAIL reads as a diagnosis and is not one; the paths are.
    check("a failed clean_tree names the paths, not the gate's prose",
          "FAIL attempt-2 - failed the gate: clean_tree - 1 path(s) left"
          " uncommitted: ?? tests/test_s2.py" in log,
          next((l for l in log.splitlines() if "FAIL attempt-2" in l), "(no line)"))
    check("...and does not print the misleading label",
          "failed the gate: tree clean (the worker committed)" not in log)
    # A reflect may rewrite the plan and commit it; a diagnostic is asked to
    # explain a failing test. Neither can do that under `acceptEdits`, which
    # accepts an edit and refuses every shell command for want of a human.
    for kind, path in (("reflect", out / "reflect-1" / "reflect.log"),
                       ("diagnostic", out / "s2" / "diagnostic.log")):
        argv_line = path.read_text(encoding="utf-8").splitlines()[0] if path.exists() else ""
        check(f"a {kind} worker is spawned with bypassPermissions",
              "--permission-mode bypassPermissions" in argv_line, argv_line[:200])
    # No worker kind may reach a tool that acts outside the tree - publishing,
    # scheduling, messaging, fanning out - because the git undo does not reach
    # there and nobody is awake to see it. Every kind, not just the read-only
    # review, and the flag appears ONCE: passed twice, which of the two the CLI
    # keeps would be the runner's business and it is not.
    for kind, path in (("build", out / "s1" / "attempt-1.log"),
                       ("review", out / "review-s1" / "review.log"),
                       ("reflect", out / "reflect-1" / "reflect.log"),
                       ("diagnostic", out / "s2" / "diagnostic.log")):
        argv_line = path.read_text(encoding="utf-8").splitlines()[0] if path.exists() else ""
        missing = [t for t in ("Artifact", "CronCreate", "SendMessage", "Workflow")
                   if t not in argv_line]
        check(f"a {kind} worker cannot reach the outward-facing tools",
              not missing and "--disallowedTools" in argv_line,
              f"missing {missing} in {argv_line[:250]}")
        check(f"the {kind} worker gets one --disallowedTools",
              argv_line.count("--disallowedTools") == 1, argv_line[:250])
    # ...and the review keeps its read-only fence on top of that list.
    review_argv = (out / "review-s1" / "review.log").read_text(encoding="utf-8").splitlines()[0]
    check("a review worker is still denied Edit, Write and NotebookEdit",
          all(t in review_argv for t in ("Edit", "Write", "NotebookEdit")),
          review_argv[:250])
    # Every worker kind is told to prefer Read/Grep/Glob over Bash for
    # exploration - a `cat`/`sed` read costs far more input tokens than the
    # equivalent structured call. One log per kind is enough to prove it is
    # not just the build brief that carries it.
    for kind, path in (("build", out / "s1" / "attempt-1.log"),
                       ("review", out / "review-s1" / "review.log"),
                       ("reflect", out / "reflect-1" / "reflect.log"),
                       ("diagnostic", out / "s2" / "diagnostic.log")):
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        check(f"the {kind} brief carries the tool-usage note",
              "Reserve Bash for" in text, f"missing in {path}")
        # ...and the half of it that costs the most: a tool result is paid for
        # on arrival and again on every call after it.
        check(f"the {kind} brief tells the worker to keep tool results small",
              "Keep tool results small" in text and "offset and limit" in text,
              f"missing in {path}")
    check("reflect-1 REFLECT CHANGED", outcome("reflect-1") == "REFLECT CHANGED", outcome("reflect-1"))
    check("added step ran and passed", outcome("added-by-reflect-1") == "PASS",
          outcome("added-by-reflect-1"))
    check("s3 PASS", outcome("s3") == "PASS", outcome("s3"))
    check("reflect-2 REVERTED (tamper)", outcome("reflect-2") == "REFLECT REVERTED", outcome("reflect-2"))
    plan_after = spec_text(repo)
    check("tamper undone in spec", "TAMPERED" not in plan_after)
    check("added step persists in spec", "added-by-reflect-1" in plan_after)
    check("tree clean at end", sh(repo, "git", "status", "--porcelain").stdout.strip() == "")
    commits = sh(repo, "git", "log", "--oneline").stdout.splitlines()
    check("plan rewrite committed", any("rewrote the plan" in c for c in commits))
    check("rework commit present", any("reworked" in c for c in commits))
    check("SUMMARY.md written", (out / "SUMMARY.md").exists())
    # The number that decides whether to launch the next stretch, on the line
    # the morning reads first.
    # BOTH totals, each named. The ledger's sum covers every step the plan has
    # ever run; under `--only` or a resume that is not what this session cost,
    # and the number that decides whether to launch the next stretch is.
    check("the last line carries this session's cost and the plan's, distinctly",
          re.search(r"run finished \(this session: \$\d+\.\d\d over \d+ step\(s\)"
                    r" \| whole plan to date: \$\d+\.\d\d over \d+ recorded\)", log)
          is not None,
          next((l for l in log.splitlines() if "run finished" in l), "(none)"))
    # The runner records the tier it ASKED for; the commit trailer names
    # whatever the child session's own attribution names, which on 2026-09-06
    # was Opus for a step spawned sonnet/medium.
    # This also pins DEFAULT_TIERS: the fixture sets no `defaults`, so a build
    # step lands on whatever the runner's default is. It read opus/medium until
    # 2026-09-08 and sonnet/medium after, and the check failing is how a change
    # to that default announces itself rather than passing silently.
    check("the ledger records the tier the runner asked for (the build default)",
          steps.get("s1", {}).get("tier") == "sonnet/medium",
          str(steps.get("s1", {}).get("tier")))
    check("...for a review step too, at its own tier",
          steps.get("review:s1", {}).get("tier") == "opus/high",
          str(steps.get("review:s1", {}).get("tier")))
    # The heartbeat writes every beat to the file and echoes every tenth. The
    # fake worker never runs for a minute, so the cadence cannot be exercised
    # end to end; the mechanism it depends on is tested directly instead.
    beat_log = Log(root / "beat" / "run.log")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        beat_log("echoed line")
        beat_log("quiet line", echo=False)
    beat_log.close()
    written = (root / "beat" / "run.log").read_text(encoding="utf-8")
    check("a quiet log line reaches the file", "quiet line" in written, written)
    check("...and does not reach stdout", "quiet line" not in captured.getvalue()
          and "echoed line" in captured.getvalue(), captured.getvalue())
    check("compact transcript flagged the error", "TOOL RESULT (error)" in
          (out / "s2" / "diagnostic.log").read_text(encoding="utf-8", errors="replace"))
    check("runner exit 0 (only benign outcomes)" if not failures else "runner exit code",
          done.returncode in (0, 1), str(done.returncode))

    tags = sh(repo, "git", "tag").stdout.split()
    check("discarded attempt tagged for rescue",
          any(t.startswith("rescue/s2-attempt") for t in tags), str(tags))
    check("rescue tag names a real commit",
          all(sh(repo, "git", "rev-parse", "--verify", t + "^{commit}",
                 check=False).returncode == 0
              for t in tags if t.startswith("rescue/")))
    check("the discarded commit is listed for the operator",
          "did work but missed the contract" in
          (out / "s2" / "discarded-commits.md").read_text(encoding="utf-8"))
    check("worker commits carry the run's committer identity",
          "overnight+selftest@runner.invalid" in
          sh(repo, "git", "log", "--format=%ce").stdout)
    check("expected_min carried into the summary",
          "| est |" in (out / "SUMMARY.md").read_text(encoding="utf-8"))

    print("2b. the plan file is the ledger")
    text = spec_text(repo)
    check("no state.json is written at all", not (out / "state.json").exists())
    check("the operator's comment survived every splice",
          "A COMMENT THE LEDGER MUST NOT DESTROY" in text)
    check("a done: block per step that ran",
          all("done:" in block for sid, block in step_blocks_text(repo).items()
              if sid in steps), sorted(steps))
    check("exactly one done: per step",
          all(block.count("\n    done:") <= 1
              for block in step_blocks_text(repo).values()))
    check("the rework REPLACED the reviewed step's done:, not added one",
          steps["s1"].get("reworked") is True and
          step_blocks_text(repo)["s1"].count("outcome:") == 1,
          step_blocks_text(repo)["s1"])
    check("a done: carries the shape the roadmap specified",
          set(steps["s1"]) >= {"outcome", "at", "sha", "minutes"}, str(steps["s1"]))
    check("the summary is beside the logs, not in the yaml",
          (out / "s3" / "summary.md").exists() and "summary:" not in text)
    # Moving the summary out of the ledger silently emptied it in the review
    # brief, because the brief still asked the ledger for it. The reviewer's
    # only account of what the worker thought it built must still reach it.
    check("the review brief carries the worker's own summary",
          "s1: pass" in (out / "review-s1" / "review.log").read_text(
              encoding="utf-8", errors="replace").split("=== WORKER OUTPUT ===")[0],
          (out / "review-s1" / "review.log").read_text(
              encoding="utf-8", errors="replace")[:400])
    ledger_commits = [c for c in sh(repo, "git", "log", "--format=%s").stdout.splitlines()
                      if c.startswith("overnight: ")]
    check("one ledger commit per recorded step",
          len(ledger_commits) >= len(steps), f"{len(ledger_commits)} for {len(steps)}")
    check("ledger commits carry the run's committer identity",
          sh(repo, "git", "log", "--format=%s%x1f%ce").stdout.count(
              "overnight: ") == len(ledger_commits) and
          all("overnight+selftest@runner.invalid" in line
              for line in sh(repo, "git", "log", "--format=%s%x1f%ce").stdout.splitlines()
              if line.startswith("overnight: ")))
    summary = (out / "SUMMARY.md").read_text(encoding="utf-8")
    check("cost totalled from the ledger", "Cumulative worker cost" in summary)
    # NOTHING A WORKER SPENT MAY GO MISSING. A rework is built by run_build,
    # which charges it to the step it repairs - a step recorded long before,
    # whose step_cost was popped with it - so the rework's spend used to sit in
    # the dict until the process exited and never reached the ledger at all.
    # The invariant is the guard: the ledger total equals what the logs say the
    # workers cost, whatever path spawned them.
    spent = 0.0
    for path in sorted(out.rglob("*.log")):
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if '"total_cost_usd"' in line:
                try:
                    spent += float(json.loads(line).get("total_cost_usd") or 0)
                except (ValueError, AttributeError):
                    pass
                break
    ledgered = sum(float(e.get("cost_usd") or 0) for e in steps.values())
    check("every worker's spend reaches the ledger, the rework's included",
          abs(ledgered - spent) < 0.005 and spent > 0,
          f"ledger ${ledgered:.2f} vs logs ${spent:.2f}")
    # ...and the morning gets the split, because the efficiency bar is about
    # structure - review, reflect and rework - not about per-call cost.
    check("SUMMARY.md splits out what was not building",
          "was not building" in summary and "reworked after a review" in summary,
          summary[-400:])

    print("3. resume skips passed steps")
    done = run_runner(repo, scenario_path)
    check("resume: nothing to do, exit 0/1", done.returncode in (0, 1))
    check("resume: no worker started", "worker starting" not in done.stdout)


    c.shared["repo"] = repo
    c.shared["log"] = log


def section_4(c):
    root = c.root
    check = c.check
    scenario_path = c.scenario_path

    print("4. a project with no git: degraded, not refused")
    plain = make_plain_dir(root)
    done = run_runner(plain, scenario_path)
    plain_out = plain / "overnight" / "runs" / "selftest"
    plain_log = (plain_out / "run.log").read_text(encoding="utf-8") \
        if (plain_out / "run.log").exists() else ""
    pledger = ledger(plain)

    def poutcome(sid):
        return pledger.get(sid, {}).get("outcome", "-")

    check("no-git: did not refuse to start", "REFUSING TO START" not in plain_log,
          plain_log[-300:])
    check("no-git: warned once", plain_log.count("RUNNING WITHOUT A GIT UNDO") == 1,
          str(plain_log.count("RUNNING WITHOUT A GIT UNDO")))
    check("no-git: names the reason", "no git repository at or above the spec" in plain_log)
    check("no-git: builds still ran", poutcome("s1") == "PASS", poutcome("s1"))
    check("no-git: clean_tree gate skipped",
          "(skipped: no git repository" in
          (plain_out / "preflight.log").read_text(encoding="utf-8"))
    check("no-git: review skipped for want of a commit",
          poutcome("review:s1") == "SKIPPED", poutcome("review:s1"))
    check("no-git: cannot reset is said plainly", "CANNOT RESET" in plain_log)
    check("no-git: SUMMARY carries the warning",
          "RUNNING WITHOUT A GIT UNDO" in
          (plain_out / "SUMMARY.md").read_text(encoding="utf-8"))



def section_5(c):
    root = c.root
    check = c.check

    print("5. a third party commits during a step: refuse, never destroy")
    # The runner does not own the branch. This is the guard for the one failure
    # that is invisible after the fact: a branch that has quietly lost a commit
    # looks exactly like a branch that never had one.
    shared = make_repo(root / "shared-branch")
    (shared / "overnight" / "steps.yaml").write_text(FOREIGN_SPEC, encoding="utf-8")
    sh(shared, "git", "add", "-A")
    sh(shared, "git", "commit", "-q", "-m", "foreign-case spec")
    foreign_scenario = root / "scenario-foreign.json"
    foreign_scenario.write_text(json.dumps({"s1": ["foreign-commit"]}), encoding="utf-8")
    done = run_runner(shared, foreign_scenario)
    fout = shared / "overnight" / "runs" / "foreign" / "s1"
    flog = (shared / "overnight" / "runs" / "foreign" / "run.log").read_text(encoding="utf-8")
    subjects = sh(shared, "git", "log", "--format=%s").stdout

    check("foreign commit SURVIVES on the branch",
          "a commit from another window" in subjects, subjects[:200])
    check("the stranger's file is still on disk",
          (shared / "someone_elses_work.md").exists())
    check("the reset was refused, loudly", "REFUSING TO RESET" in flog)
    check("the step halted rather than rewriting history",
          "HALTED" in flog, flog[-400:])
    check("the foreign commit is named in the step directory",
          "FOREIGN" in (fout / "discarded-commits.md").read_text(encoding="utf-8"))
    check("the run did not pretend to succeed", done.returncode == 1, str(done.returncode))


    c.shared["shared"] = shared


def section_6(c):
    check = c.check
    repo = c.shared["repo"]
    shared = c.shared["shared"]

    print("6. --progress digests a run without needing the spec")
    digest = subprocess.run([sys.executable, "-u", str(RUNNER), "--progress"],
                            cwd=repo, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    check("--progress exits 0", digest.returncode == 0, digest.stderr[-300:])
    check("--progress names the run", "run `selftest`" in digest.stdout)
    check("--progress reports the outcomes", "s1" in digest.stdout and
          "PASS" in digest.stdout)
    # The finished/in-flight verdict is read out of run.log by matching the
    # last line the runner writes. That line now carries the run's cost total
    # between the words and the colon, and matching the colon would silently
    # report a finished run as IN FLIGHT.
    check("--progress sees a finished run as FINISHED",
          "status: FINISHED" in digest.stdout,
          next((l for l in digest.stdout.splitlines() if "status:" in l), "(none)"))
    check("--progress flags what needs the operator",
          "NEEDS YOU" in subprocess.run(
              [sys.executable, "-u", str(RUNNER), "--progress"], cwd=shared,
              capture_output=True, text=True, encoding="utf-8",
              errors="replace").stdout)



def section_7(c):
    root = c.root
    check = c.check
    repo = c.shared["repo"]
    shared = c.shared["shared"]

    print("7. --mode decides which way /overnight goes, from the plan alone")
    empty = root / "no-plan"
    empty.mkdir()
    check("no plan -> PLAN", mode(empty) == ("PLAN", 0), str(mode(empty)))
    check("every step complete -> REPLACE?", mode(repo) == ("REPLACE?", 0),
          str(mode(repo)))
    # `shared` ended with its only step HALTED, from section 5.
    check("a HALTED step -> BLOCKED", mode(shared) == ("BLOCKED", 3), str(mode(shared)))
    blocked_out = subprocess.run([sys.executable, "-u", str(RUNNER), "--mode"],
                                 cwd=shared, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
    check("BLOCKED names the step and says why it needs a person",
          "s1" in blocked_out and "need a person" in blocked_out, blocked_out[:200])
    fresh = make_repo(root / "not-yet-run")
    check("a plan with nothing run -> RUN", mode(fresh) == ("RUN", 0), str(mode(fresh)))



def section_8(c):
    root = c.root
    check = c.check

    print("8. the splice itself")
    # A hand edit to a PENDING step while the run is live must survive the next
    # record: the runner re-reads the file at record time rather than writing
    # back a copy taken at step start.
    fresh = make_repo(root / "splice")
    before_text = spec_text(fresh)
    edited = before_text.replace("title: passes first time",
                                 "title: EDITED BY HAND MID-RUN")
    (fresh / "overnight" / "steps.yaml").write_text(edited, encoding="utf-8")
    spliced = upsert_done(edited, "s3", {"outcome": "PASS", "at": "2026-01-01 00:00"})
    check("a hand edit to another step survives a splice",
          "EDITED BY HAND MID-RUN" in spliced)
    check("the splice put done: on the step it was told to",
          "done:" in _blocks_of(spliced)["s3"] and "done:" not in _blocks_of(spliced)["s1"])
    check("a note full of YAML metacharacters cannot break the file",
          yaml.safe_load(upsert_done(edited, "s1", {
              "outcome": "STUCK",
              "note": 'failed: "x: y" #comment \\ - [{&*]'})) is not None)
    twice = upsert_done(upsert_done(edited, "s1", {"outcome": "PASS"}),
                        "s1", {"outcome": "STUCK"})
    check("a second record REPLACES the first", twice.count("outcome:") == 1
          and "STUCK" in twice, twice[:400])



def section_9(c):
    check = c.check
    repo = c.shared["repo"]

    print("9. --reset-state forgets every outcome, and launches nothing")
    # No --only guard here, deliberately. This once read `--only
    # nonexistent-step`, which gave the runner an empty queue and so hid the
    # defect it should have caught: --reset-state was handled as a side effect
    # of building the Runner and then fell through into a real launch. Without
    # the guard the old code re-runs the whole plan against the fake worker.
    before_head = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    log_path = repo / "overnight" / "runs" / "selftest" / "run.log"
    before_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    done = subprocess.run([sys.executable, "-u", str(RUNNER), "--spec",
                           "overnight/steps.yaml", "--fake-worker", str(FAKE),
                           "--reset-state"],
                          cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    after_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    new_log = after_log[len(before_log):] if after_log.startswith(before_log) else after_log
    new_commits = sh(repo, "git", "log", "--format=%s",
                     f"{before_head}..HEAD").stdout.splitlines()
    check("--reset-state exits 0", done.returncode == 0,
          f"rc={done.returncode} {done.stdout[-300:]}")
    check("--reset-state started no step", "] start (" not in new_log,
          new_log[:400])
    check("--reset-state made the reset commit and nothing else",
          len(new_commits) == 1, str(new_commits))
    check("--reset-state says nothing was launched",
          "Nothing was launched" in done.stdout, done.stdout[-300:])
    # A `done:` KEY, not the string - the fixture's own comment mentions `done:`
    # and matching that made this assertion pass for the wrong reason.
    left = [l for l in spec_text(repo).splitlines() if re.match(r"^\s*done:\s*$", l)]
    check("--reset-state leaves no done: in the plan", not left, str(left))
    check("--reset-state kept the operator's comment",
          "A COMMENT THE LEDGER MUST NOT DESTROY" in spec_text(repo))
    check("--reset-state committed the change",
          any("--reset-state" in c for c in
              sh(repo, "git", "log", "--format=%s").stdout.splitlines()))
    check("after --reset-state the mode is RUN again", mode(repo) == ("RUN", 0),
          str(mode(repo)))



def section_10(c):
    root = c.root
    check = c.check
    scenario_path = c.scenario_path

    print("10. --dry-run touches nothing")
    # A repo where nothing has run, so every named gate FAILS - no worker has
    # written the file it tests. That is the ordinary case for a rehearsal the
    # night before, and it is where the runner used to exhaust three stub
    # attempts, record a real committed STUCK, and leave the plan BLOCKED.
    rehearsal = make_repo(root / "rehearsal")
    before_text = spec_text(rehearsal)
    before_head = sh(rehearsal, "git", "rev-parse", "HEAD").stdout.strip()
    done = run_runner(rehearsal, scenario_path, "--dry-run")
    dry_out = rehearsal / "overnight" / "runs" / "selftest" / "dry-run"
    dry_log = (dry_out / "run.log").read_text(encoding="utf-8") \
        if (dry_out / "run.log").exists() else ""
    check("--dry-run exits 0", done.returncode == 0,
          f"rc={done.returncode} {done.stdout[-300:]}")
    check("--dry-run left the plan byte-identical",
          spec_text(rehearsal) == before_text)
    check("--dry-run committed nothing",
          sh(rehearsal, "git", "rev-parse", "HEAD").stdout.strip() == before_head)
    check("after a --dry-run the mode is still RUN", mode(rehearsal) == ("RUN", 0),
          str(mode(rehearsal)))
    # The gates must still RUN - that is what a rehearsal is for. It is the
    # RECORDING of their verdict that was wrong.
    attempt_1 = dry_out / "s1" / "attempt-1.log"
    check("--dry-run still ran the gates",
          attempt_1.exists() and "=== GATES ===" in
          attempt_1.read_text(encoding="utf-8"), f"no {attempt_1}")
    check("--dry-run says the plan was not written",
          "the plan is NOT written" in dry_log, dry_log[-400:])
    check("--dry-run does not call a rehearsal STUCK",
          "STUCK" not in dry_log and "DRY RUN" in dry_log, dry_log[-400:])
    # Both halves: a missing dry-run directory would satisfy "no attempt-2" for
    # entirely the wrong reason.
    check("--dry-run stops after one attempt",
          attempt_1.exists() and not (dry_out / "s1" / "attempt-2.log").exists())
    # safe_reset quarantines untracked files and resets the tree to get a clean
    # one. Nothing ran, so there is nothing to undo and nothing of the
    # operator's to move.
    check("--dry-run left the working tree untouched",
          not sh(rehearsal, "git", "status", "--porcelain").stdout.strip(),
          sh(rehearsal, "git", "status", "--porcelain").stdout[:200])
    # Not a run: `--progress` must not report the rehearsal as one.
    check("--dry-run is not mistaken for a run",
          not (rehearsal / "overnight" / "runs" / "selftest" / "run.log").exists())
    progress = subprocess.run([sys.executable, "-u", str(RUNNER), "--progress"],
                              cwd=rehearsal, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout
    check("--progress after a dry run says nothing has started",
          "(not started)" in progress, progress[:300])



def section_11(c):
    root = c.root
    check = c.check

    print("11. a report writes nothing at all")
    # Building a Runner opened the run log, so --list and --print-brief left an
    # empty run.log in a project that had never run a step - and find_runs looks
    # for exactly that file, so --progress then reported a run IN FLIGHT that
    # had never started. A report must leave the tree byte-for-byte as it was.
    for label, argv in (("--list", ["--list"]),
                        ("--print-brief", ["--print-brief", "s1"])):
        quiet = make_repo(root / f"report{label.strip('-')}")
        paths_before = sorted(p.relative_to(quiet).as_posix()
                              for p in quiet.rglob("*") if ".git" not in p.parts)
        head_before = sh(quiet, "git", "rev-parse", "HEAD").stdout.strip()
        done = subprocess.run([sys.executable, "-u", str(RUNNER), "--spec",
                               "overnight/steps.yaml", "--fake-worker", str(FAKE),
                               *argv],
                              cwd=quiet, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        paths_after = sorted(p.relative_to(quiet).as_posix()
                             for p in quiet.rglob("*") if ".git" not in p.parts)
        check(f"{label} exits 0", done.returncode == 0, done.stderr[-200:])
        check(f"{label} creates no file or directory", paths_before == paths_after,
              str([p for p in paths_after if p not in paths_before]))
        check(f"{label} commits nothing",
              sh(quiet, "git", "rev-parse", "HEAD").stdout.strip() == head_before)
        after = subprocess.run([sys.executable, "-u", str(RUNNER), "--progress"],
                               cwd=quiet, capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stdout
        check(f"--progress after {label} does not invent a run",
              "(not started)" in after, after[:200])



def section_12(c):
    root = c.root
    check = c.check
    scenario_path = c.scenario_path

    print("12. one runner per repository")
    locked = make_repo(root / "locked")
    lock = locked / "overnight" / "runs" / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # A live pid this test controls: its own. The runner must believe it.
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                "run": "pretend", "spec": "overnight/steps.yaml",
                                "started": "2026-01-01 00:00:00"}), encoding="utf-8")
    done = run_runner(locked, scenario_path)
    check("a second runner refuses to start", done.returncode == 2, done.stdout[-300:])
    check("...and says which run holds it, and where the lock is",
          "pretend" in done.stdout and str(lock) in done.stdout, done.stdout[-300:])
    check("...and did not take the lock for itself",
          json.loads(lock.read_text(encoding="utf-8"))["run"] == "pretend")
    # A pid that is not running is a crashed run, not a live one.
    lock.write_text(json.dumps({"pid": 999999999, "host": socket.gethostname(),
                                "run": "crashed", "spec": "overnight/steps.yaml",
                                "started": "2026-01-01 00:00:00"}), encoding="utf-8")
    done = run_runner(locked, scenario_path)
    check("a stale lock is taken over, not obeyed",
          "taking over a stale lock" in done.stdout, done.stdout[:400])
    check("the lock is given back when the run ends", not lock.exists())
    # A report is not a run and must not queue behind one.
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                "run": "pretend", "spec": "overnight/steps.yaml",
                                "started": "2026-01-01 00:00:00"}), encoding="utf-8")
    listed = subprocess.run([sys.executable, "-u", str(RUNNER), "--spec",
                             "overnight/steps.yaml", "--fake-worker", str(FAKE), "--list"],
                            cwd=locked, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    check("--list works while a run holds the lock", listed.returncode == 0,
          listed.stdout[-200:])
    check("...and left the lock alone",
          json.loads(lock.read_text(encoding="utf-8"))["run"] == "pretend")



def section_13(c):
    root = c.root
    check = c.check

    print("13. worktree isolation")

    def isolated_repo(name, behaviour):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(ISOLATED_SPEC, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "isolated spec")
        scen = root / f"scenario-{name}.json"
        scen.write_text(json.dumps({"s1": [behaviour]}), encoding="utf-8")
        return where, run_runner(where, scen)

    # THE demonstration. This is the case that HALTs today: a third party
    # commits to the branch mid-step. Isolated, the worker never shared the
    # tree, so the work replays onto the moved branch and the step passes.
    iso, done = isolated_repo("isolated", "pass+main:unrelated")
    ilog = (iso / "overnight" / "runs" / "isolated" / "run.log").read_text(encoding="utf-8")
    isubjects = sh(iso, "git", "log", "--format=%s").stdout
    check("an isolated step runs on its own branch", "isolated on `overnight/" in ilog,
          ilog[:600])
    check("a third party commit mid-step no longer HALTs the step",
          "HALTED" not in ilog and "PASS" in ilog, ilog[-500:])
    check("the stranger's commit is still on the branch",
          "a commit from another window" in isubjects, isubjects[:200])
    check("the worker's work was replayed onto it, not discarded",
          "fake: s1 built" in isubjects, isubjects[:300])
    check("the run succeeded", done.returncode == 0, ilog[-300:])
    check("the worktree was removed at the end",
          not any((root / "isolated").glob("*.overnight-worktrees/*/s1")),
          str(sorted(p.name for p in (root / "isolated").glob("*.overnight-worktrees/*/*"))))
    # Not "equals HEAD" - the ledger commit lands after it. The point is that
    # the sha is REACHABLE from the branch: a replay rewrites the worktree's
    # sha, and recording the pre-replay one would name a commit the morning
    # cannot look up.
    on_branch = sh(iso, "git", "rev-list", "HEAD").stdout
    recorded = ledger(iso)["s1"].get("sha") or "(none)"
    check("the recorded sha is a commit on the operator's branch",
          any(line.startswith(recorded) for line in on_branch.splitlines()),
          f"{recorded} not in the branch's history")
    check("...and the note names that same commit, not the worktree's",
          recorded in str(ledger(iso)["s1"].get("note")), str(ledger(iso)["s1"]))

    # The same, but the stranger writes the file the worker is writing.
    conf, done = isolated_repo("isolated-conflict", "pass+main:conflict")
    clog = (conf / "overnight" / "runs" / "isolated" / "run.log").read_text(encoding="utf-8")
    branches = sh(conf, "git", "branch", "--list", "overnight/*").stdout
    check("work that cannot be replayed is NEEDS MERGE, not discarded",
          ledger(conf).get("s1", {}).get("outcome") == "NEEDS MERGE",
          str(ledger(conf).get("s1")))
    check("the work is kept on its scratch branch", "overnight/isolated/s1" in branches,
          branches)
    check("...and the branch really holds the commit",
          "fake: s1 built" in sh(conf, "git", "log", "--format=%s",
                                 "overnight/isolated/s1").stdout)
    check("a NEEDS MERGE stops the run", "could not be merged" in clog, clog[-400:])
    check("...and blocks the next /overnight", mode(conf) == ("BLOCKED", 3),
          str(mode(conf)))
    check("...and is not re-run on the next launch",
          not is_resumable({"id": "s1", "done": {"outcome": "NEEDS MERGE"}}))

    # The gate needs something git does not carry. It must fail HERE, at
    # preflight, not at 03:00 as three failed attempts and a STUCK step.
    gitignored = make_repo(root / "isolated-deps")
    (gitignored / "overnight" / "steps.yaml").write_text(
        ISOLATED_SPEC.replace("cmd: python -m pytest -q tests/test_base.py",
                              "file: local-only.txt"), encoding="utf-8")
    (gitignored / ".gitignore").write_text("overnight/runs/\nlocal-only.txt\n",
                                           encoding="utf-8")
    (gitignored / "local-only.txt").write_text("not in git\n", encoding="utf-8")
    sh(gitignored, "git", "add", "-A")
    sh(gitignored, "git", "commit", "-q", "-m", "spec with a gitignored dependency")
    scen = root / "scenario-deps.json"
    scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
    done = run_runner(gitignored, scen)
    check("a gate needing a gitignored path refuses AT PREFLIGHT",
          done.returncode == 2 and "FAIL in a fresh worktree" in done.stdout,
          done.stdout[-400:])
    check("...and says what to do about it",
          "worktree_link" in done.stdout and "in-place" in done.stdout,
          done.stdout[-300:])
    check("...and started no step", "] start (" not in done.stdout, done.stdout[-300:])



def section_14(c):
    root = c.root
    check = c.check

    print("14. the default is isolation, and it is what makes a stray survivable")
    # THE FIELD DEFECT of 2026-09-06. A file appears in the operator's tree
    # while a step runs - their own session log, in the incident - after the
    # step's untracked snapshot was taken. The worker never saw it and its
    # brief told it to leave such a file alone; in-place the clean_tree gate
    # failed the step for it anyway, discarded a good build, and relaunched
    # into the same unwinnable state until the attempts ran out.

    def stray_repo(name, spec_text_):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(spec_text_, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "stray spec")
        scen = root / f"scenario-{name}.json"
        scen.write_text(json.dumps({"s1": ["pass+main:stray"]}), encoding="utf-8")
        return where, run_runner(where, scen)

    stray, done = stray_repo("stray-default", DEFAULT_ISO_SPEC)
    slog = (stray / "overnight" / "runs" / "default-iso" / "run.log").read_text(
        encoding="utf-8")
    check("a spec that says nothing about isolation gets a worktree",
          "isolated on `overnight/" in slog, slog[:600])
    check("a file written into the operator's tree mid-step does not fail the step",
          ledger(stray).get("s1", {}).get("outcome") == "PASS",
          str(ledger(stray).get("s1")))
    check("...the run finishes clean", done.returncode == 0, slog[-400:])
    check("...the work is on the branch",
          "fake: s1 built" in sh(stray, "git", "log", "--format=%s").stdout)
    # Left ALONE, not quarantined: it is the operator's file, in the operator's
    # tree, and no part of this run has any business moving it.
    check("...and the operator's file is untouched where they left it",
          (stray / "Session history" / "notes.md").exists()
          and "written mid-step" in (stray / "Session history" / "notes.md").read_text(
              encoding="utf-8"))

    # The same run in-place, which is why the default changed. This is not a
    # bug being asserted as correct - in-place genuinely cannot tell the
    # worker's uncommitted work from a third party's file, and this is the
    # price of that ambiguity, kept in view so that flipping the default back
    # cannot be done quietly.
    old, done = stray_repo("stray-in-place",
                           DEFAULT_ISO_SPEC.replace("  attempts: 2",
                                                    "  attempts: 2\n  isolation: in-place"))
    olog = (old / "overnight" / "runs" / "default-iso" / "run.log").read_text(
        encoding="utf-8")
    check("in-place, the same stray still costs the step",
          ledger(old).get("s1", {}).get("outcome") == "STUCK",
          str(ledger(old).get("s1")))
    check("...on every attempt, not just the first",
          olog.count("failed the gate") >= 2, olog[-600:])
    check("...and the discarded build is recoverable, not gone",
          (old / "overnight" / "runs" / "default-iso" / "s1"
           / "discarded-commits.md").exists()
          and "fake: s1 built" in sh(old, "git", "tag", "-l").stdout + olog, olog[-600:])



def section_15(c):
    root = c.root
    check = c.check

    print("15. the usage wall: park or stop, but never march through the plan")
    # THE INCIDENT OF 2026-09-07, reproduced. The account's five-hour window
    # closed mid-run; every worker after it returned nothing in seconds, and
    # the runner - unable to tell "this step failed" from "nothing can succeed"
    # - retried, diagnosed, marked STUCK and moved on, thirty-odd times. The
    # morning showed a plan whose every step needed re-running and a summary
    # full of findings about code no worker had ever read.

    def wall_repo(name, scenario, *extra, spec=WALL_SPEC):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(spec, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "wall spec")
        scen = root / f"scenario-{name}.json"
        scen.write_text(json.dumps(scenario), encoding="utf-8")
        done_ = run_runner(where, scen, *extra)
        wlog = (where / "overnight" / "runs" / "wall" / "run.log").read_text(
            encoding="utf-8")
        return where, done_, wlog

    walled, done, wlog = wall_repo(
        "wall-stop", {"s1": ["wall"], "s1#diag": ["wall"]}, "--on-wall", "stop")
    wsteps = ledger(walled)
    # NOT `STUCK`. A STUCK step has had three real attempts and a diagnostic
    # and is a finding about the work; this one was never tested at all, and
    # calling it STUCK is what made the morning unreadable.
    check("a walled step is NOT RUN, not STUCK",
          wsteps.get("s1", {}).get("outcome") == "NOT RUN",
          str(wsteps.get("s1")))
    check("...and says the environment was the reason, not the code",
          "NOTHING WAS LEARNED ABOUT THIS STEP" in str(wsteps.get("s1", {}).get("note")),
          str(wsteps.get("s1", {}).get("note")))
    # The whole point: the run does not go on to burn the rest of the plan.
    check("the next step is never started", "s2" not in wsteps, str(wsteps))
    check("...and the run says so, naming what is left",
          "the environment stopped answering" in wlog and "s2" in wlog, wlog[-600:])
    check("the wall was called on consecutive empty workers, not an error string",
          "returned NOTHING (exit 1, no result event)" in wlog, wlog[-800:])
    check("a NOT RUN step is resumable, not blocking",
          is_resumable(yaml.safe_load(spec_text(walled))["steps"][0]))
    check("--mode does not report a walled plan as BLOCKED",
          subprocess.run([sys.executable, str(RUNNER), "--mode", str(walled)],
                         capture_output=True, text=True).stdout.strip() != "BLOCKED")
    wsummary = (walled / "overnight" / "runs" / "wall" / "SUMMARY.md").read_text(
        encoding="utf-8")
    # Under `stop` nothing is ever parked, so this sentence must NOT hang off
    # the parked note - which is exactly where it was, leaving NOT RUN standing
    # in the table with nothing saying the environment caused it.
    check("SUMMARY says the untested step is pending, not failed",
          "pending, not failed" in wsummary
          and "never really attempted" in wsummary, wsummary[:1200])

    # And the default: park, probe, and carry on. The account comes back on the
    # second probe, and the step that was never really attempted then runs for
    # real - which is the behaviour that makes an overnight run survive a
    # window boundary instead of being timed around one.
    parked, done, plog = wall_repo(
        "wall-park",
        {"s1": ["wall", "wall", "wall", "pass"], "s1#diag": ["wall"],
         "_probe": ["wall", "probe-ok"]},
        "--park-poll-min", "0.02")
    psteps = ledger(parked)
    check("parked rather than stopping (the default)", "PARKED after s1" in plog,
          plog[-800:])
    check("...probed cheaply instead of retrying the step",
          "probe 1: still walled" in plog and "probe 2: ALIVE" in plog, plog[-900:])
    check("...and the probe is a haiku worker, not the step's own brief",
          "--model haiku" in (parked / "overnight" / "runs" / "wall" / "probes"
                              / "probe-01.log").read_text(encoding="utf-8"))
    check("...resumed the step it was on", "RESUMING" in plog, plog[-800:])
    check("...which then ran for real and passed",
          psteps.get("s1", {}).get("outcome") == "PASS", str(psteps.get("s1")))
    check("...and the rest of the plan ran too",
          psteps.get("s2", {}).get("outcome") == "PASS", str(psteps.get("s2")))
    check("...with the run finishing clean", done.returncode == 0, plog[-400:])
    psummary = (parked / "overnight" / "runs" / "wall" / "SUMMARY.md").read_text(
        encoding="utf-8")
    # Every per-hour figure in the summary is wall-clock, so a run that spent
    # part of the night waiting has to say so or the numbers lie.
    check("SUMMARY declares the time that went to waiting",
          "was PARKED" in psummary and "did not extend the stop time" in psummary,
          psummary[:900])

    # BOTH progress readers, against a log cut off mid-park. A parked run's
    # last `start (` line is the step it stopped on and can be hours old; read
    # as "in flight" it is indistinguishable from a hang, which is the single
    # thing parking must never look like - and the 3am question this project
    # already learned to answer is exactly "is it stuck?".
    pdir = parked / "overnight" / "runs" / "wall"
    cut = plog[:plog.index("RESUMING")]
    assert "PARKED after" in cut and "run finished" not in cut
    (pdir / "run.log").write_text(cut, encoding="utf-8")
    # The lock is the REPOSITORY's, beside the run directories, not inside one.
    (parked / "overnight" / "runs" / ".lock").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8")
    prog = subprocess.run([sys.executable, str(RUNNER), "--progress", str(parked)],
                          capture_output=True, text=True, encoding="utf-8").stdout
    check("--progress calls a parked run parked, not in flight",
          "PARKED - waiting for the account, not stuck" in prog
          and "in flight" not in prog, prog[-600:])
    one = subprocess.run([sys.executable, str(HERE / "tools" / "progress.py"),
                          str(parked)], capture_output=True, text=True,
                         encoding="utf-8").stdout
    check("the one-line report says PARKED, not the step it stopped on",
          "PARKED (waiting for the account, not stuck)" in one, one)

    # THE BREAKER'S BLIND SPOT (F2b). The probe answers - the account is up -
    # and this one step's workers still return nothing, which is the shape of a
    # mistyped `model`, `effort` or budget: the CLI rejects the flag before it
    # makes an API call, so the worker dies with no result event, which is
    # exactly what a usage wall looks like. Before this, the run parked, put the
    # step back at the head of the queue, went barren again, and parked again,
    # every park_poll_min until morning - a whole night spent on one typo, with
    # the rest of the plan never started.
    # `--hours 0.08` BOUNDS THE FAILURE, and is the only reason this test can be
    # left in a suite people wait for: with the fix it finishes in seconds, and
    # if the fix ever regresses the run parks in a loop and is stopped by the
    # clock five minutes later instead of running until the spec's hour is up.
    # Five and not three: the bound has to clear two full rounds of s1 - three
    # attempts each, two pytest gates an attempt - a park, and then s2's own
    # minute, or a loaded machine fails the `takes the next step` check below
    # as a clock artefact rather than a regression.
    barren, done, blog = wall_repo(
        "barren-step",
        {"s1": ["wall"], "s1#diag": ["wall"], "s2": ["pass"], "_probe": ["probe-ok"]},
        "--hours", "0.08", spec=BARREN_SPEC)
    bsteps = ledger(barren)
    check("a step that stays barren while the probe answers is BARREN, not NOT RUN",
          bsteps.get("s1", {}).get("outcome") == "BARREN", str(bsteps.get("s1")))
    check("...and the note blames the step, not the environment",
          "That is THIS STEP, not the environment"
          in str(bsteps.get("s1", {}).get("note")), str(bsteps.get("s1", {}).get("note")))
    check("...and carries the command that produced nothing, and what it said",
          "--model" in str(bsteps.get("s1", {}).get("note"))
          and "session limit" in str(bsteps.get("s1", {}).get("note")),
          str(bsteps.get("s1", {}).get("note")))
    # The whole value: the night is not spent on one step.
    check("...so the run takes the next step instead", bsteps.get("s2", {}).get("outcome") == "PASS",
          str(bsteps.get("s2")))
    check("...having parked exactly once, not once per attempt at it",
          blog.count("PARKED after") == 1, blog[-900:])
    check("...and the barren count is reset, so s2 is not walled on its first worker",
          "[s2] PASS" in blog, blog[-600:])
    check("BARREN needs a person: --mode says BLOCKED",
          subprocess.run([sys.executable, str(RUNNER), "--mode", str(barren)],
                         capture_output=True, text=True).stdout.strip().splitlines()[0]
          == "BLOCKED")
    check("...and is re-run once they have changed something",
          is_resumable(yaml.safe_load(spec_text(barren))["steps"][0]))
    check("...and the run exits non-zero", done.returncode == 1, blog[-400:])
    bsummary = (barren / "overnight" / "runs" / "wall" / "SUMMARY.md").read_text(
        encoding="utf-8")
    check("SUMMARY says which keys to look at, rather than leaving BARREN bare",
          "`BARREN`" in bsummary and "`effort`" in bsummary, bsummary[:1400])
    # THE THIRD BARREN WORKER OF THE CASCADE. The diagnostic reads a compact
    # transcript of attempts 1 and 2 - and when both returned nothing, both
    # transcripts are the CLI's own error message. An opus worker was being paid
    # to summarise "session limit", into whatever had stopped the other two.
    check("no diagnostic is spawned over two empty transcripts",
          "SKIPPING the diagnostic" in blog
          and not (barren / "overnight" / "runs" / "wall" / "s1" / "diagnostic.log").exists(),
          blog[-1200:])

    # G-4. A reflect worker that never answered leaves the plan untouched, and
    # "untouched" was recorded as REFLECT NO CHANGE: benign for the exit code,
    # complete on a resume, and in the morning it reads as *the plan was
    # considered and found sound*. Measured on 2026-09-07 (`reflect-3`, NO
    # CHANGE, $0.00). The wall is never reached here - one barren reflect is
    # enough, which is the point.
    refl, rdone, rlog = wall_repo(
        "barren-reflect", {"s1": ["pass"], "reflect-1": ["wall"]},
        spec=BARREN_REFLECT_SPEC)
    rsteps = ledger(refl)
    check("a reflect whose worker returned nothing is INCONCLUSIVE, not NO CHANGE",
          rsteps.get("reflect-1", {}).get("outcome") == "REFLECT INCONCLUSIVE",
          str(rsteps.get("reflect-1")))
    check("...and says the plan was not considered",
          "this is not `no change`" in str(rsteps.get("reflect-1", {}).get("note")),
          str(rsteps.get("reflect-1", {}).get("note")))
    check("...is not complete: a resume runs it again",
          is_resumable([s for s in yaml.safe_load(spec_text(refl))["steps"]
                        if s["id"] == "reflect-1"][0]))
    check("...and the run does not exit 0 over it", rdone.returncode == 1, rlog[-400:])



def section_16(c):
    root = c.root
    check = c.check

    print("16. a worker's text cannot be printed: degrade, never die")
    # THE CRASH OF 2026-09-07. A worker's own summary carried a `<=`; the
    # 02:00 scheduled task ran under Task Scheduler's cmd.exe at cp1252; the
    # runner died in `print`, in Log.__call__, AFTER the worker had finished
    # and committed but BEFORE the outcome was recorded. The work survived
    # orphaned on a scratch branch and the plan said nothing had happened -
    # the worst shape a failure can take, because the morning cannot see it.
    #
    # The console's encoding belongs to whoever launched the run and worker
    # text is arbitrary, so this can never be fixed by writing careful ASCII
    # in this repository. It is fixed by making the ECHO lossy.
    uni = make_repo(root / "cp1252")
    (uni / "overnight" / "steps.yaml").write_text(DEFAULT_ISO_SPEC, encoding="utf-8")
    sh(uni, "git", "add", "-A")
    sh(uni, "git", "commit", "-q", "-m", "unicode spec")
    uscen = root / "scenario-cp1252.json"
    uscen.write_text(json.dumps({"s1": ["pass-unicode"]}), encoding="utf-8")
    uenv = dict(os.environ)
    uenv["OVERNIGHT_FAKE_SCENARIO"] = str(uscen)
    # THE WHOLE POINT OF THE CASE. Not a decoration: with utf-8 here, as every
    # other case in this file uses, the defect is invisible.
    uenv["PYTHONIOENCODING"] = "cp1252"
    done = subprocess.run(
        [sys.executable, "-u", str(RUNNER), "--spec", "overnight/steps.yaml",
         "--fake-worker", str(FAKE)],
        cwd=uni, env=uenv, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    check("a cp1252 console does not kill the run",
          "UnicodeEncodeError" not in done.stdout + done.stderr,
          (done.stdout + done.stderr)[-500:])
    check("...the step still completes and is recorded",
          ledger(uni).get("s1", {}).get("outcome") == "PASS",
          str(ledger(uni).get("s1")))
    check("...the run reaches its end rather than unwinding",
          done.returncode == 0, done.stdout[-300:])
    # The FILE is opened utf-8 independently, so degrading the echo costs
    # nothing: what the worker actually said is still on disk in full.
    ulog = (uni / "overnight" / "runs" / "default-iso" / "run.log").read_text(
        encoding="utf-8")
    check("...and run.log keeps the characters the console could not show",
          "\u2264" in ulog and "\u00e9" in ulog, ulog[-300:])



def section_17(c):
    root = c.root
    check = c.check

    print("17. a worktree an earlier run left behind")
    # THE FIELD DEFECT of 2026-09-07. `git worktree list --porcelain` prints
    # FORWARD slashes; `str(path)` on Windows gives BACKslashes, so the
    # membership test in prune_worktrees was ALWAYS true and it deleted the
    # directory of every REGISTERED worktree under its root. `git worktree
    # prune` had already run by then, so the registration was left dangling
    # and the next `worktree add -B` failed "already used by worktree": the
    # step went STUCK with zero attempts, having read no code and spent
    # nothing. It cost a live run its night's work.
    def leftover(name, then=None):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(ISOLATED_SPEC, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "isolated spec")
        wroot = where.parent / f"{where.name}.overnight-worktrees" / "isolated"
        sh(where, "git", "worktree", "add", "-q", "-B",
           "overnight/isolated/s1", str(wroot / "s1"), "HEAD")
        if then:
            then(wroot)
        scen = root / f"scenario-{name}.json"
        scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
        done = run_runner(where, scen)
        wlog = (where / "overnight" / "runs" / "isolated" / "run.log").read_text(
            encoding="utf-8")
        return where, wroot, done, wlog

    # (a) The registered leftover is still on disk. It must be handed to git
    # to remove, never torn off the filesystem behind git's back.
    live, wroot, done, wlog = leftover("wt-live")
    check("a registered leftover is not treated as an orphan",
          "orphaned worktree" not in wlog, wlog[:800])
    check("...and the step runs rather than going STUCK at zero attempts",
          ledger(live).get("s1", {}).get("outcome") == "PASS",
          str(ledger(live).get("s1")))
    check("...with no `already used by worktree` anywhere in the run",
          "already used by worktree" not in wlog, wlog[-600:])
    check("...and the run ends cleanly", done.returncode == 0, wlog[-300:])

    # (b) The directory was torn off behind git's back by something else -
    # the state the defect used to leave. The run must recover, not stall.
    dang, wroot, done, wlog = leftover(
        "wt-dangling", lambda r: shutil.rmtree(r / "s1", ignore_errors=True))
    check("a dangling registration with no directory does not stall the step",
          ledger(dang).get("s1", {}).get("outcome") == "PASS",
          str(ledger(dang).get("s1")))

    # (c) The function's actual job, which the fix must not lose: a directory
    # under the root that git knows nothing about IS an orphan, and goes.
    def stray(r):
        (r / "old-step").mkdir(parents=True, exist_ok=True)
        (r / "old-step" / "junk.txt").write_text("from a crashed run\n", encoding="utf-8")

    orph, wroot, done, wlog = leftover("wt-orphan", stray)
    check("an unregistered directory under the root is still removed",
          not (wroot / "old-step").exists(),
          str(sorted(p.name for p in wroot.iterdir())) if wroot.exists() else "(gone)")
    check("...and is named in the log as an orphan",
          "orphaned worktree" in wlog, wlog[:800])
    check("...and the step still passes",
          ledger(orph).get("s1", {}).get("outcome") == "PASS",
          str(ledger(orph).get("s1")))



def section_18(c):
    root = c.root
    check = c.check

    print("18. work a crashed run left on a scratch branch is rescued, not reset away")
    # `worktree add -B` FORCE-MOVES the branch to base. A crashed run's only
    # copy of its work is a commit on that branch, so re-running the step
    # made it unreachable - no tag, no note, nothing for the morning to find.
    # safe_reset has tagged before discarding since the beginning; this is
    # the other place the runner moves a ref, and it did not.
    crashed = make_repo(root / "wt-rescue")
    (crashed / "overnight" / "steps.yaml").write_text(ISOLATED_SPEC, encoding="utf-8")
    sh(crashed, "git", "add", "-A")
    sh(crashed, "git", "commit", "-q", "-m", "isolated spec")
    cwt = crashed.parent / f"{crashed.name}.overnight-worktrees" / "isolated" / "s1"
    sh(crashed, "git", "worktree", "add", "-q", "-B", "overnight/isolated/s1",
       str(cwt), "HEAD")
    (cwt / "half-done.txt").write_text("what the crashed run had built\n", encoding="utf-8")
    sh(cwt, "git", "add", "-A")
    sh(cwt, "git", "commit", "-q", "-m", "work from a run that crashed")
    orphaned = sh(cwt, "git", "rev-parse", "HEAD").stdout.strip()
    sh(crashed, "git", "worktree", "remove", "--force", str(cwt))
    scen = root / "scenario-wt-rescue.json"
    scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
    done = run_runner(crashed, scen)
    tags = sh(crashed, "git", "tag", "--list", "rescue/*").stdout
    check("the orphaned commit is tagged before the branch is reset",
          "rescue/s1/scratch-1" in tags, tags or "(no rescue tags)")
    check("...and the tag really names that commit",
          sh(crashed, "git", "rev-parse", "rescue/s1/scratch-1^{commit}",
             check=False).stdout.strip() == orphaned, orphaned)
    disc = crashed / "overnight" / "runs" / "isolated" / "s1" / "discarded-commits.md"
    check("...and the step directory says what was on the branch and how to get it",
          disc.exists() and orphaned in disc.read_text(encoding="utf-8")
          and "cherry-pick" in disc.read_text(encoding="utf-8"),
          disc.read_text(encoding="utf-8")[:400] if disc.exists() else "(no file)")
    check("...and the step itself still runs",
          ledger(crashed).get("s1", {}).get("outcome") == "PASS",
          str(ledger(crashed).get("s1")))
    # It is DISCARDED because it failed, not because it was stranded. This
    # fixture's commit writes `half-done.txt` and never the test the step's gate
    # runs, so the gate is what condemns it - and the gate has to be asked first,
    # or good work is thrown away with the bad. Section 23 is the other half.
    crash_log = (crashed / "overnight" / "runs" / "isolated" / "run.log").read_text(
        encoding="utf-8")
    check("...having first been replayed and put through the gates",
          (crashed / "overnight" / "runs" / "isolated" / "s1" / "stranded.log").exists(),
          "no stranded.log")
    check("...and the log says the gates are what condemned it",
          "FAILS this step's gates" in crash_log, crash_log[-500:])



def section_19(c):
    root = c.root
    check = c.check

    print("19. a worker woken by its own background task is counted once, in full")
    # FinKit `5b-recognise`, 2026-09-07: logged `turns=4 $14.53` for a step
    # that really took 123 turns over two sessions. The runner read the LAST
    # result event, which belonged to the 4-turn tail. tools/tally.py had the
    # same bug from the other side and costed the tail's usage alone - $0.54
    # against a real $14.53, a 27x under-count, which is worse than a crash
    # because nobody goes looking for a number that merely looks small.
    woke = make_repo(root / "rewoken")
    (woke / "overnight" / "steps.yaml").write_text(DEFAULT_ISO_SPEC, encoding="utf-8")
    sh(woke, "git", "add", "-A")
    sh(woke, "git", "commit", "-q", "-m", "rewoken spec")
    wscen = root / "scenario-rewoken.json"
    wscen.write_text(json.dumps({"s1": ["pass-rewoken"]}), encoding="utf-8")
    done = run_runner(woke, wscen)
    wlog = (woke / "overnight" / "runs" / "default-iso" / "run.log").read_text(
        encoding="utf-8")
    check("turns are SUMMED over every session in the log",
          "turns=123" in wlog, wlog[-600:])
    check("...and not read off the last one alone",
          "turns=4 " not in wlog and "turns=119" not in wlog, wlog[-600:])
    # Cost is cumulative over the process, so summing would double it to
    # $28.52. Measured on the real log before this fixture was written.
    check("cost is NOT double-counted: the last result carries the whole spend",
          "$14.53" in wlog and "$28.52" not in wlog, wlog[-600:])
    check("the step itself still passes",
          ledger(woke).get("s1", {}).get("outcome") == "PASS",
          str(ledger(woke).get("s1")))
    tally = subprocess.run(
        [sys.executable, "-u", str(HERE / "tools" / "tally.py"),
         str(woke / "overnight" / "runs" / "default-iso")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    # 18,344,715 + 889,344 cache-read tokens. Asserted as TOKENS, not dollars,
    # so the guard cannot be moved by a price change; the tail alone is 889k.
    check("tally sums every session's usage, not the last one's",
          "19234k" in tally.stdout, tally.stdout[:600])



def section_20(c):
    root = c.root
    check = c.check

    print("20. a stalled worker is killed; a merely slow one is not")
    # 2026-09-07: two workers went silent with their log size frozen and each
    # burned the full 90-minute worker_timeout_min before exit 124. The retry
    # then passed in 27 and 47 minutes - so the retry was always the fix, and
    # the only cost was the waiting. worker_timeout_min cannot catch this: it
    # has to be set for the slowest legitimate step.
    #
    # The threshold is measured, not guessed. Across 25 real worker logs the
    # largest silence a WORKING worker produced was 291s; the median per-log
    # maximum was 81s. Both slow paths keep the log growing - a Bash call over
    # ~30s emits a tool_progress heartbeat, a long generation emits
    # thinking_tokens - so silence really does mean wedged.
    def stall_repo(name, behaviour, *extra):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(DEFAULT_ISO_SPEC,
                                                        encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "stall spec")
        scen = root / f"scenario-{name}.json"
        scen.write_text(json.dumps({"s1": [behaviour, "pass"]}), encoding="utf-8")
        done = run_runner(where, scen, *extra)
        wlog = (where / "overnight" / "runs" / "default-iso" / "run.log").read_text(
            encoding="utf-8")
        return where, done, wlog

    # 0.05 min = 3s, so the case runs in seconds rather than ten minutes.
    wedged, done, wlog = stall_repo("stalled", "stall", "--stall-min", "0.05")
    check("a worker that writes nothing is killed", "STALLED" in wlog, wlog[-700:])
    check("...and is not left to burn the whole worker timeout",
          "KILLED after" not in wlog, wlog[-500:])
    check("...and the attempt is spent, not the run",
          ledger(wedged).get("s1", {}).get("outcome") == "PASS",
          str(ledger(wedged).get("s1")))
    check("...so the retry - which is the fix - actually ran",
          "attempt-2" in wlog, wlog[-500:])
    # A stalled worker exits non-zero with no result event, which is exactly
    # the shape of a barren one. It must not be read as the usage wall.
    # Not "PARK not in the log" - the startup banner names the wall policy on
    # every run, so that string is there before anything has happened.
    check("a stall is never counted as a barren worker",
          "returned NOTHING" not in wlog, wlog[-600:])
    check("...so the run never parks on it",
          "PARKED" not in wlog and "waits here" not in wlog, wlog[-600:])

    # THE OTHER HALF, and the one that matters more: a slow step must survive.
    slow, done, wlog = stall_repo("slow", "slow-but-alive", "--stall-min", "0.05")
    check("a slow worker that keeps writing is NOT killed",
          "STALLED" not in wlog, wlog[-700:])
    check("...and its step passes on the first attempt",
          ledger(slow).get("s1", {}).get("outcome") == "PASS"
          and ledger(slow).get("s1", {}).get("attempts") == 1,
          str(ledger(slow).get("s1")))

    # And the watchdog can be turned off outright.
    off, done, wlog = stall_repo("stall-off", "slow-but-alive", "--stall-min", "0")
    check("--stall-min 0 disables the watchdog", "STALLED" not in wlog, wlog[-400:])



def section_21(c):
    root = c.root
    check = c.check

    print("21. a worker cut off by the per-step budget is not retried")
    # `--max-budget-usd` is per INVOCATION, so retrying a cap trip spends the
    # cap again to be cut off in the same place: three attempts against a $6
    # cap billed $18 and learned nothing. The cause is not something the
    # worker got wrong - the brief asks for more than the cap will pay for -
    # so it is a planning failure and it needs a person, not a retry.
    def budget_repo(name, behaviour):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(BUDGET_SPEC, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "budget spec")
        scen = root / f"scenario-{name}.json"
        # A SECOND behaviour that passes: pre-fix, attempt 2 ran and the step
        # ended PASS, so the guard fails against the old runner rather than
        # merely passing for a different reason.
        scen.write_text(json.dumps({"s1": [behaviour, "pass"]}), encoding="utf-8")
        done = run_runner(where, scen)
        wlog = (where / "overnight" / "runs" / "budget" / "run.log").read_text(
            encoding="utf-8")
        return where, done, wlog

    broke, done, wlog = budget_repo("budget", "over-budget")
    entry = ledger(broke).get("s1", {})
    check("a budget trip gets its own outcome, not STUCK",
          entry.get("outcome") == "OVER BUDGET", str(entry))
    check("...after ONE attempt, not three",
          entry.get("attempts") == 1, str(entry))
    check("...so no second worker is ever spawned",
          not (broke / "overnight" / "runs" / "budget" / "s1" / "attempt-2.log").exists())
    check("...and no diagnostic is run over it either",
          not (broke / "overnight" / "runs" / "budget" / "s1" / "diagnostic.log").exists())
    check("the note names the spend against the cap",
          "$6.02 against a $6.00 cap" in str(entry.get("note")), str(entry.get("note")))
    # All THREE levers, because which one applies is the user's call and a note
    # naming only one of them is an instruction rather than a choice.
    check("...and says what a person is being asked to change",
          all(s in str(entry.get("note")) for s in
              ("Split the step", "budget_usd", "run.continuations")),
          str(entry.get("note")))
    check("...and says continuation was off, not that it was tried and failed",
          "continuation is off" in str(entry.get("note")), str(entry.get("note")))
    # It reported a cost and turns, so it is a worker that answered. Reading it
    # as barren would park the run on a spending limit that is the run's own.
    check("a budget trip is never counted as a barren worker",
          "returned NOTHING" not in wlog, wlog[-600:])
    # Half-done work: the fixture writes the test file and never commits it.
    check("the half-finished tree is cleaned up",
          not sh(broke, "git", "status", "--porcelain").stdout.strip(),
          sh(broke, "git", "status", "--porcelain").stdout)
    check("--mode reports the plan as BLOCKED - it needs a person",
          subprocess.run([sys.executable, str(RUNNER), "--mode", str(broke)],
                         capture_output=True, text=True).stdout.strip().startswith("BLOCKED"))
    check("...and it is still resumable once they have changed something",
          is_resumable(yaml.safe_load(spec_text(broke))["steps"][0]))

    # THE OTHER HALF: the cap tripping AFTER the work is committed and the
    # gates pass is a PASS. The gates are the arbiter, not the exit code -
    # otherwise a step whose work is on the branch gets thrown away for
    # running out of money on its way out.
    late, done, wlog = budget_repo("budget-late", "over-budget-late")
    entry = ledger(late).get("s1", {})
    check("a cap trip after the gates pass is still a PASS",
          entry.get("outcome") == "PASS", str(entry))
    check("...on the first attempt, with nothing redone",
          entry.get("attempts") == 1, str(entry))

    # F3: `budget_usd` on a REVIEW step must reach ITS OWN worker too -
    # `worker_argv` otherwise fell back silently to the run-level cap for
    # review, reflect and diagnostic, so a planner who gave one of them a
    # larger cap than the builds (as the README invites) got the build cap
    # instead.
    review_budget = SPEC.replace(
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: rework\n",
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: rework\n"
        "    budget_usd: 3\n")
    where = make_repo(root / "review-budget")
    (where / "overnight" / "steps.yaml").write_text(review_budget, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "review budget spec")
    scen = root / "scenario-review-budget.json"
    scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
    done = run_runner(where, scen)
    review_log = (where / "overnight" / "runs" / "selftest" / "review-s1"
                 / "review.log").read_text(encoding="utf-8")[:2000]
    check("a review step's own budget_usd is what reaches --max-budget-usd",
          "--max-budget-usd 3" in review_log, review_log[:300])



def section_22(c):
    root = c.root
    check = c.check

    print("22. a cut-off worker is CONTINUED, not repeated")
    # The cap is per invocation, so the money a retry spends buys the same
    # work again. A continuation spends it on the part that is not done: the
    # next worker gets a fresh cap and the tree exactly as the last left it.
    # The guard that matters is the runaway one - a worker that spent the cap
    # and changed nothing has produced nothing to hand on, and continuing it
    # would buy a second cap of going nowhere.
    def continue_repo(name, behaviours, also=None):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(CONTINUE_SPEC, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "continue spec")
        scen = root / f"scenario-{name}.json"
        plan = dict(also or {})
        plan["s1"] = behaviours
        scen.write_text(json.dumps(plan), encoding="utf-8")
        done = run_runner(where, scen)
        out = where / "overnight" / "runs" / "continue"
        return where, done, (out / "run.log").read_text(encoding="utf-8"), out

    went, done, wlog, out = continue_repo(
        "continued", ["over-budget", "finish-the-handover"])
    entry = ledger(went).get("s1", {})
    # `finish-the-handover` REFUSES unless the half-built file is still there
    # and the brief says it is a continuation, so a PASS here is the proof
    # that the tree was carried forward and the handover was written.
    check("a continuation finishes the work the cap cut short",
          entry.get("outcome") == "PASS", str(entry))
    check("...within ONE attempt, because a continuation is not a retry",
          entry.get("attempts") == 1, str(entry))
    check("...and the ledger says two workers did it",
          entry.get("legs") == 2, str(entry))
    check("...with the second one's log kept beside the first",
          (out / "s1" / "attempt-1-continued-1.log").exists())
    # Asserted on the run log, not the ledger note: under isolation the note is
    # REPLACED at integration with where the commit landed on the branch, which
    # is the more useful thing for it to say. `legs:` carries the count.
    check("the log says the budget cut the step short",
          "over 2 workers; the budget cut short 1" in wlog, wlog[-800:])
    # THE HANDOVER ITSELF, in the brief the second worker was actually given.
    # Read defensively: if the continuation never ran, that is a FAILED CHECK
    # above, and the suite must go on to report it rather than dying here.
    cont_log = out / "s1" / "attempt-1-continued-1.log"
    handover = cont_log.read_text(encoding="utf-8") if cont_log.exists() else ""
    check("the continuation is told not to start over",
          "Do not start over" in handover, handover[:400])
    check("...and is given what the last worker left in the tree",
          "Left uncommitted in the tree:" in handover, handover[:400])

    # THE PER-STEP CAP: the run says 6, the step says 2, and 2 must be what the
    # worker was SPAWNED with, not merely what the note later claims. Asserted
    # against the argv line the runner writes at the head of every worker log.
    spawned = (out / "s1" / "attempt-1.log").read_text(encoding="utf-8")[:2000]
    check("a step's own budget_usd is what reaches --max-budget-usd",
          "--max-budget-usd 2" in spawned, spawned[:300])
    check("...and the run-level cap it overrides is not passed as well",
          "--max-budget-usd 6" not in spawned, spawned[:300])

    # THE RUNAWAY: cap spent, tree byte-identical. Nothing to hand on.
    idle, done, wlog, out = continue_repo(
        "runaway", ["over-budget-idle", "finish-the-handover"])
    entry = ledger(idle).get("s1", {})
    check("a worker that spent the cap and changed NOTHING is not continued",
          entry.get("outcome") == "OVER BUDGET", str(entry))
    check("...so no second worker is spawned for it",
          not (out / "s1" / "attempt-1-continued-1.log").exists())
    check("...and the note says why it was not continued",
          "changed NOTHING" in str(entry.get("note")), str(entry.get("note")))

    # THE BOUND: continuations is 1, so two workers and no more, however many
    # times the cap is tripped. Otherwise this is the $18 money pump again.
    pump, done, wlog, out = continue_repo(
        "bounded", ["over-budget", "over-budget", "over-budget"])
    entry = ledger(pump).get("s1", {})
    check("continuation is bounded: one extra worker, not an open cheque",
          entry.get("outcome") == "OVER BUDGET" and entry.get("legs") == 2,
          str(entry))
    check("...and it stops at the limit rather than retrying the attempt",
          not (out / "s1" / "attempt-2.log").exists())
    check("...saying the continuations were used up",
          "continuation(s) were used up" in str(entry.get("note")),
          str(entry.get("note")))

    # A CONTINUATION OF A REWORK must still carry the review's findings. The
    # handover brief is built on the brief the cut-off worker actually had -
    # composing a fresh attempt-1 brief instead would silently drop `rework`
    # (and, on attempt 3, the diagnostic's remediation), so the continuation
    # would carry on building the very thing the reviewer had just rejected
    # with nothing in front of it saying so.
    red, done, wlog, out = continue_repo(
        "rework-continued", ["pass"],
        {"review:s1": ["review:rework"],
         "s1#rework": ["over-budget", "finish-the-handover"]})
    check("a rework can be continued too",
          ledger(red).get("review:s1", {}).get("outcome") == "REVIEW REWORK PASS",
          str(ledger(red).get("review:s1")))
    cont = out / "s1" / "rework-continued-1.log"
    check("...and its continuation still carries the review's findings",
          cont.exists() and "# REWORK:" in cont.read_text(encoding="utf-8"),
          "no continuation log" if not cont.exists() else "no REWORK heading")


# The suite, in the order it runs, with what each section needs before it and how
# many checks it is supposed to make.
#
# `needs` is not documentation. Fifteen of these sections build their own
# repository and stand alone; the handful that do not read a fixture an earlier
# section left in a particular state, and asking for one of those on its own runs
# its prerequisites too rather than a subtly different test.
#
# `checks` is the guard for the one failure this file cannot otherwise see. The
# sections are selected from this table, so a section dropped from it - or a check
# lost inside one - still ends with `SELFTEST PASS` and a quietly weaker suite.
# The count turns that silence into a failure. When you add a check, the run tells
# you the new number; put it here.
def section_23(c):
    root = c.root
    check = c.check

    print("23. work an earlier run stranded is RETESTED, not thrown away")
    # WOODWORK GURU, 2026-09-07. `b3-4-nested-drawer`'s worker finished at
    # 02:32:39 - exit 0, 11.3 min, $5.12, gates passed, committed - and the run
    # died before integrating it. The relaunch could not create the worktree and
    # recorded `STUCK, attempts: 0, could not create the worktree for this step`,
    # which reads as NOTHING HAPPENED. A complete, gates-passing step sat on
    # `overnight/engine-b0-b8/b3-4-nested-drawer`, and the natural next move -
    # re-run it - rebuilds work that already exists and already passed.
    #
    # Section 18 is the other half of this and is unchanged: stranded work that
    # FAILS its gates is still tagged and discarded. What is new is that the gate
    # is asked, because everywhere else in this runner the gate is already the
    # arbiter of whether work is good.
    def stranded(name, build, then=None):
        """A repository whose scratch branch holds work no run ever integrated."""
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(ISOLATED_SPEC, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "isolated spec")
        wt = where.parent / f"{where.name}.overnight-worktrees" / "isolated" / "s1"
        sh(where, "git", "worktree", "add", "-q", "-B", "overnight/isolated/s1",
           str(wt), "HEAD")
        build(wt)
        sh(wt, "git", "add", "-A")
        sh(wt, "git", "commit", "-q", "-m", "work from a run that crashed")
        sha = sh(wt, "git", "rev-parse", "HEAD").stdout.strip()
        sh(where, "git", "worktree", "remove", "--force", str(wt))
        if then:
            then(where)
        scen = root / f"scenario-{name}.json"
        # A worker that WOULD have passed, so "no worker was spawned" is a real
        # assertion rather than a step that had no way to succeed anyway. Against
        # the old runner this case passes with attempts: 1 and an attempt-1.log.
        scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
        done_ = run_runner(where, scen)
        return where, sha, done_, (where / "overnight" / "runs" / "isolated"
                                   / "run.log").read_text(encoding="utf-8")

    # (a) It passes the step's own gates, so it IS the work the step asked for.
    # It merges as a successful re-run's work would, and no worker is spent: in
    # the field case that is 11 minutes and $5.12 recovered for nothing.
    def good(wt):
        (wt / "tests" / "test_s1.py").write_text("def test_s1():\n    assert 1\n",
                                                 encoding="utf-8")
        (wt / "built.txt").write_text("what the crashed run had built\n",
                                      encoding="utf-8")

    kept, sha, done, slog = stranded("stranded-passes", good)
    entry = ledger(kept).get("s1", {})
    sout = kept / "overnight" / "runs" / "isolated" / "s1"
    check("stranded work that passes its gates is a PASS",
          entry.get("outcome") == "PASS", str(entry))
    check("...with NO worker spawned for the step",
          entry.get("attempts") == 0 and not (sout / "attempt-1.log").exists(),
          f"attempts={entry.get('attempts')}")
    check("...and the note says the work came from an earlier run",
          "NO WORKER WAS SPENT" in str(entry.get("note")), str(entry.get("note")))
    check("...the work really is on the operator's branch",
          (kept / "built.txt").exists() and (kept / "tests" / "test_s1.py").exists())
    check("...the gates were run against it, and their log kept",
          (sout / "stranded.log").exists() and "=== GATES ===" in
          (sout / "stranded.log").read_text(encoding="utf-8"), "no stranded.log")
    check("...and it was not tagged and discarded",
          not sh(kept, "git", "tag", "--list", "rescue/*").stdout.strip(),
          sh(kept, "git", "tag", "--list", "rescue/*").stdout)
    check("...with the run ending clean", done.returncode == 0, slog[-400:])

    # (c) The branch moved under it and the work will not replay. That is the one
    # case a person is needed for, and NEEDS MERGE already means exactly it.
    def conflicting(wt):
        (wt / "shared.txt").write_text("what the crashed run wrote\n", encoding="utf-8")

    def the_operator_writes_it_too(where):
        (where / "shared.txt").write_text("what the operator wrote\n", encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "the operator's own commit")

    stuck, sha, done, slog = stranded("stranded-conflicts", conflicting,
                                      the_operator_writes_it_too)
    entry = ledger(stuck).get("s1", {})
    check("stranded work that will not replay is NEEDS MERGE",
          entry.get("outcome") == "NEEDS MERGE", str(entry))
    check("...saying it could not be TESTED, not that it failed",
          "do not replay" in str(entry.get("note")), str(entry.get("note")))
    check("...the commit is still on its branch, untouched",
          sh(stuck, "git", "rev-parse", "overnight/isolated/s1").stdout.strip() == sha,
          sh(stuck, "git", "rev-parse", "overnight/isolated/s1").stdout)
    check("...and the operator's own commit survived",
          "the operator wrote" in (stuck / "shared.txt").read_text(encoding="utf-8"))
    check("...which blocks the next /overnight", mode(stuck) == ("BLOCKED", 3),
          str(mode(stuck)))


def section_24(c):
    root = c.root
    check = c.check

    print("24. the clock: --until, and the stop it enforces")
    # THE CLOCK HAD NO TEST AT ALL until this section, which is the only path in
    # the runner that can end a run with steps still pending and no failure to
    # show for it. It is also the one setting an operator gets wrong silently:
    # `--hours 7` is arithmetic done once, at launch, against a number that is
    # only ever a proxy for the time of day they actually mean - and every minute
    # spent typing the command comes off the end of the run.

    # -- what a time means ------------------------------------------------
    # Against a FIXED reference, not `now`. A test whose expected answer is
    # computed from the clock it is testing proves only that two copies of the
    # same arithmetic agree, and one run at 23:59 would behave differently from
    # every other run of the suite.
    ref = dt.datetime(2026, 9, 7, 23, 0)
    check("`07:30` at 23:00 means TOMORROW morning, which is what it was typed to mean",
          parse_until("07:30", ref) == dt.datetime(2026, 9, 8, 7, 30))
    check("`23:30` at 23:00 means tonight - the next occurrence, not always tomorrow",
          parse_until("23:30", ref) == dt.datetime(2026, 9, 7, 23, 30))
    check("a dated time is taken literally",
          parse_until("2026-09-09 06:15", ref) == dt.datetime(2026, 9, 9, 6, 15))
    # A DATED stop time in the past is a typo, and rolling it forward a day the
    # way a bare `HH:MM` is rolled would hide the typo behind a run that looked
    # fine. A bare time cannot be a typo in that sense - it has no date to be
    # wrong about.
    refused = None
    try:
        parse_until("2026-09-06 06:15", ref)
    except SpecError as exc:
        refused = str(exc)
    check("a dated time already past is REFUSED, not rolled forward a day",
          refused is not None and "past" in refused, f"got {refused!r}")
    nonsense = None
    try:
        parse_until("tomorrow morning", ref)
    except SpecError as exc:
        nonsense = str(exc)
    check("a value that is not a time is refused, and the message says both forms",
          nonsense is not None and "HH:MM" in nonsense and "YYYY-MM-DD" in nonsense,
          f"got {nonsense!r}")

    # -- which of four sources wins ---------------------------------------
    def prep(name, text):
        """A repository with a spec of our choosing, committed."""
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(text, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "spec", check=False)
        return where

    def launch(where, *extra):
        """(the finished process, the banner's clock line)."""
        done = run_runner(where, c.scenario_path, *extra)
        return done, next((ln for ln in done.stdout.splitlines()
                           if "the clock:" in ln), "")

    def clock_line(name, text, *extra):
        where = prep(name, text)
        done, line = launch(where, *extra)
        return where, done, line

    now = dt.datetime.now()
    later = (now + dt.timedelta(hours=3)).replace(second=0, microsecond=0)
    other = (now + dt.timedelta(hours=5)).replace(second=0, microsecond=0)
    spec_until = SPEC.replace("hours: 1", 'until: "%s"' % other.strftime("%H:%M"))
    spec_neither = SPEC.replace("  hours: 1\n", "")

    _, _, line = clock_line("clock-cli-until", spec_until, "--dry-run", "--only", "s1",
                            "--until", later.strftime("%H:%M"), "--hours", "9")
    check("--until beats --hours on the command line",
          "--until" in line and "--hours" not in line, f"got {line!r}")
    check("...and the banner names the absolute time it resolved to, not the flag alone",
          later.strftime("%Y-%m-%d %H:%M") in line, f"got {line!r}")

    # THE JUDGEMENT CALL, stated in the banner so it need not be guessed at:
    # the command line beats the spec FIRST, and only then does `until` beat
    # `hours` within a level. The other reading - `until` winning wherever it
    # appears - makes a spec carrying `until` silently swallow a typed --hours.
    # An operator who overrides the clock and watches the run stop at the time
    # they overrode has nothing to blame and no way to find out why.
    _, _, line = clock_line("clock-cli-hours", spec_until, "--dry-run", "--only", "s1",
                            "--hours", "9")
    check("a typed --hours beats an `until` in the spec - the flag is never inert",
          "--hours 9" in line, f"got {line!r}")

    _, _, line = clock_line("clock-spec-until", spec_until, "--dry-run", "--only", "s1")
    check("run.until in the spec is used when the command line says nothing",
          "run.until" in line and other.strftime("%Y-%m-%d %H:%M") in line, f"got {line!r}")

    _, _, line = clock_line("clock-spec-hours", SPEC, "--dry-run", "--only", "s1")
    check("run.hours in the spec is used when it has no until",
          "run.hours 1" in line, f"got {line!r}")

    _, _, line = clock_line("clock-default", spec_neither, "--dry-run", "--only", "s1")
    check("a spec with neither falls back to six hours, and SAYS it is the default",
          "default" in line and "6" in line, f"got {line!r}")

    # -- the clock stops a run --------------------------------------------
    # `--hours 0` is the deterministic form of "the time has come": no step can
    # be started, so nothing depends on how fast this machine runs a fake worker.
    where, done, _ = clock_line("clock-stops", SPEC, "--hours", "0")
    check("the clock stops the run before a single step starts",
          "STOP: the clock" in done.stdout, done.stdout[-300:])
    check("...and the STOP line names the stop time and what set it",
          "set by --hours 0" in done.stdout, done.stdout[-300:])
    check("...and names the steps that were not started, so the morning is not a puzzle",
          "s1" in done.stdout.split("not started:")[-1] if "not started:" in done.stdout
          else False)
    check("...and nothing is recorded against them: pending, not failed",
          ledger(where) == {}, str(ledger(where)))

    # The same stop, driven by --until rather than --hours.
    #
    # THE WINDOW IS MEASURED FROM THE LAUNCH, NOT FROM THE TOP OF THE SECTION.
    # It was five lines earlier at first, before `make_repo` runs four git
    # commands, and the window had already expired by the time the runner parsed
    # it - so the run was refused as already past instead of being stopped by the
    # clock. That passed run on its own and failed in the full suite, where the
    # machine is busier, which is the worst way for a test to be wrong.
    #
    # Five seconds is safe at both ends and does not race: launching a Python
    # process takes well under it, so the value is still in the future when it is
    # parsed; and six steps cannot run in it, so the clock is certain to stop the
    # queue. Which iteration it stops on does not matter and is not asserted.
    where = prep("clock-stops-until", SPEC)
    imminent = (dt.datetime.now() + dt.timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    done, _ = launch(where, "--until", imminent)
    check("--until enforces the same stop, and the log says it was --until that did it",
          "STOP: the clock" in done.stdout and "set by --until" in done.stdout,
          (done.stdout + done.stderr)[-300:])
    check("...and the plan is left with steps still to run rather than failures",
          len(ledger(where)) < 5, str(ledger(where)))

    # -- a bad --until is refused before anything happens -------------------
    where = make_repo(root / "clock-bad")
    done = run_runner(where, c.scenario_path, "--until", "2020-01-01 07:30")
    check("a stop time already past is refused rather than started",
          done.returncode != 0)
    check("...with a sentence, not a traceback - it is read at midnight, if at all",
          "Traceback" not in (done.stdout + done.stderr)
          and "already past" in (done.stdout + done.stderr),
          (done.stdout + done.stderr)[-300:])
    check("...and the refusal happens before the run directory is made",
          not (where / "overnight" / "runs" / "selftest").exists())


CLOCK_FIT_SPEC = """\
run:
  name: selftest
  hours: 1
  attempts: 1
  # Above expected_min below (600) - A2 refuses a plan whose timeout cannot
  # outlast its own estimate, and this step's whole point is that the CLOCK
  # skips it, not that its worker ever runs long enough to hit this bound.
  worker_timeout_min: 700
  isolation: in-place
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q}
    - {clean_tree: true}
steps:
  - id: s1
    kind: build
    title: too big for the time left
    brief: overnight/briefs/s1.md
    expected_min: 600
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: review:s1
    kind: review
    of: s1
    on_fail: rework
  - id: s2
    kind: build
    title: small enough to fit
    brief: overnight/briefs/s2.md
    expected_min: 1
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
"""


def section_25(c):
    root = c.root
    check = c.check

    print("25. `expected_min` is required, and it decides what can still start")

    def prep(name, text):
        where = make_repo(root / name)
        (where / "overnight" / "steps.yaml").write_text(text, encoding="utf-8")
        sh(where, "git", "add", "-A")
        sh(where, "git", "commit", "-q", "-m", "spec", check=False)
        return where

    def listing(where):
        """`--list` loads the spec and prints it, so it is the cheapest way to ask
        whether a plan is acceptable at all."""
        return subprocess.run([sys.executable, "-u", str(RUNNER), "--spec",
                               "overnight/steps.yaml", "--list"],
                              cwd=where, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    # -- an unsized build step is a PLANNING failure ------------------------
    # The alternative was to run it anyway and log that it could not be checked.
    # This placement is stronger: a step nobody can size is a step nobody scoped,
    # and letting it through to execution moves the failure somewhere it can no
    # longer be fixed. Across one 36-step plan every build step carrying an
    # estimate finished in 8-24 minutes and every step carrying none ran 28, 33,
    # 52, 53, 54, 119 and 151.
    unsized = SPEC.replace("    expected_min: 15\n", "").replace(
        "    expected_min: 12\n", "")
    done = listing(prep("size-refused", unsized))
    both = done.stdout + done.stderr
    check("a build step with no expected_min is refused, not run",
          done.returncode != 0, both[-200:])
    check("...and EVERY unsized step is named, so one edit fixes the plan",
          "s1" in both and "s2" in both, both[-300:])
    check("...and the message says what is missing and why it is a planning fault",
          "expected_min" in both and "scoped" in both, both[-300:])
    check("...as a sentence, not a traceback", "Traceback" not in both, both[-300:])

    # ONLY STEPS STILL TO RUN. A completed step's estimate is moot - its actual is
    # recorded - and rewriting history to satisfy a new rule teaches nobody
    # anything. It is also what stopped the rule stranding live plans: the two real
    # plans it was checked against had 13 and 0 unsized build steps, and 0 and 0
    # unsized AND still to run.
    already_ran = SPEC.replace("    expected_min: 15\n", "").replace(
        "    brief: overnight/briefs/s1.md\n",
        "    brief: overnight/briefs/s1.md\n    done:\n      outcome: PASS\n"
        "      minutes: 9\n")
    done = listing(prep("size-exempt-done", already_ran))
    check("an unsized step that has ALREADY RUN is exempt, and the plan loads",
          done.returncode == 0, (done.stdout + done.stderr)[-300:])
    check("...and it is still reported, with the outcome it recorded",
          re.search(r"^s1\s+build\s+PASS", done.stdout, re.M) is not None,
          done.stdout[:300])

    done = listing(prep("size-not-a-number",
                        SPEC.replace("expected_min: 15", 'expected_min: "soon"')))
    check("an expected_min that is not a positive number is refused at load",
          done.returncode != 0 and "positive" in (done.stdout + done.stderr),
          (done.stdout + done.stderr)[-200:])
    done = listing(prep("size-zero", SPEC.replace("expected_min: 15", "expected_min: 0")))
    check("...and so is zero", done.returncode != 0, (done.stdout + done.stderr)[-200:])

    # -- F2a: a mistyped effort or budget cap is refused at load, not at 2am -
    # (the second layer, the wall not looping over a step that can never
    # start, is F2b and landed in phase 2 - see docs/audit-findings.md)
    bad_step_effort = SPEC.replace(
        "    kind: build\n    title: passes first time",
        "    kind: build\n    effort: mediumish\n    title: passes first time")
    done = listing(prep("effort-bad-step", bad_step_effort))
    both = done.stdout + done.stderr
    check("a step-level effort outside low/medium/high is refused at load",
          done.returncode != 0 and "effort" in both and "mediumish" in both, both[-300:])

    bad_default_effort = SPEC.replace(
        "run:\n  name: selftest",
        "run:\n  name: selftest\n  defaults:\n    build: {effort: mediumish}")
    done = listing(prep("effort-bad-default", bad_default_effort))
    both = done.stdout + done.stderr
    check("a bad effort in run.defaults is refused the same way",
          done.returncode != 0 and "run.defaults" in both, both[-300:])

    bad_budget_per_step = SPEC.replace(
        "run:\n  name: selftest", "run:\n  name: selftest\n  budget_usd_per_step: -5")
    done = listing(prep("budget-per-step-bad", bad_budget_per_step))
    both = done.stdout + done.stderr
    check("a non-positive run.budget_usd_per_step is refused at load",
          done.returncode != 0 and "budget_usd_per_step" in both, both[-300:])

    # -- A2: a timeout that cannot outlast its own estimate is refused -------
    # Without this, a build step whose worker is killed before it could
    # plausibly finish the work it was estimated to take fails every attempt
    # regardless of what the worker does - not a timeout, a spec that cannot
    # pass.
    too_short_timeout = SPEC.replace("worker_timeout_min: 20", "worker_timeout_min: 5")
    done = listing(prep("timeout-too-short", too_short_timeout))
    both = done.stdout + done.stderr
    check("timeout_min <= expected_min is refused, not doomed to fail all night",
          done.returncode != 0 and "timeout_min" in both and "expected_min" in both,
          both[-300:])

    # -- F1: a brief or preamble path that does not exist is refused early --
    # `load_spec` only knows the key is present, not that the path resolves -
    # the repo root is only known at preflight. Without this, the worker is
    # spawned on a preamble, a header and a gate list, nothing else.
    missing_brief = SPEC.replace("brief: overnight/briefs/s1.md",
                                 "brief: overnight/briefs/nope.md")
    where = prep("brief-missing", missing_brief)
    scen = root / "scenario-brief-missing.json"
    scen.write_text("{}", encoding="utf-8")
    done = run_runner(where, scen)
    both = done.stdout + done.stderr
    check("a build step's brief that does not exist is refused before any worker starts",
          done.returncode == 2 and "nope.md" in both, both[-400:])
    check("...and no worker log was written for it",
          not (where / "overnight" / "runs" / "selftest" / "s1").exists(), "")

    missing_preamble = SPEC.replace("preamble: overnight/briefs/_preamble.md",
                                    "preamble: overnight/briefs/absent.md")
    where = prep("preamble-missing", missing_preamble)
    scen2 = root / "scenario-preamble-missing.json"
    scen2.write_text("{}", encoding="utf-8")
    done = run_runner(where, scen2)
    check("a run.preamble that does not exist is refused the same way",
          done.returncode == 2 and "absent.md" in (done.stdout + done.stderr),
          (done.stdout + done.stderr)[-400:])

    # -- a step that will not fit is skipped, not the end of the run --------
    # Stopping the run was the alternative, on the grounds that skipping can build
    # on ground that was never laid. Skipping wins because stopping throws away the
    # rest of the night over a hazard the runner can state plainly - and it states
    # it twice, in the log and in the summary, because nothing here can check it.
    scenario = root / "scenario-25.json"
    scenario.write_text(json.dumps({"s2": ["pass"]}), encoding="utf-8")
    where = prep("size-skips", CLOCK_FIT_SPEC)
    done = run_runner(where, scenario, "--hours", "1")
    entries = ledger(where)

    check("a step whose estimate exceeds the time left is NOT RUN",
          entries.get("s1", {}).get("outcome") == "NOT RUN", str(entries.get("s1")))
    note = str(entries.get("s1", {}).get("note", ""))
    check("...and the note names BOTH figures - the estimate and the time left",
          "600" in note and "min left" in note, note)
    check("...and reads as a clock decision, not as a usage-wall casualty",
          "stop time" in note and "returned nothing" not in note, note)
    check("...and no worker was spawned for it",
          entries.get("s1", {}).get("attempts") == 0, str(entries.get("s1")))
    check("a later, smaller step runs in its place",
          entries.get("s2", {}).get("outcome") == "PASS", str(entries.get("s2")))
    check("the log says the plan order was departed from, naming both steps",
          "PLAN ORDER DEPARTED FROM" in done.stdout
          and "s1" in done.stdout.split("PLAN ORDER DEPARTED FROM")[1][:200]
          and "s2" in done.stdout.split("PLAN ORDER DEPARTED FROM")[1][:200],
          done.stdout[-400:])
    check("...and says plainly that nothing verified they were independent",
          "NOTHING VERIFIES" in done.stdout, done.stdout[-400:])

    summary = (where / "overnight" / "runs" / "selftest" / "SUMMARY.md").read_text(
        encoding="utf-8")
    check("SUMMARY.md carries the departure too - the log is not read every morning",
          "THE PLAN ORDER WAS DEPARTED FROM" in summary, summary[-500:])
    check("...and names which step was skipped and which ran instead",
          "skipped `s1`, ran `s2` instead" in summary, summary[-500:])
    check("...and says the runner ASSUMED the skipped step was not a prerequisite",
          "prerequisite" in summary and "Nothing verified that" in summary,
          summary[-500:])

    spec_now = yaml.safe_load(spec_text(where))
    skipped_step = next(s for s in spec_now["steps"] if s["id"] == "s1")
    check("the skipped step is RESUMABLE - a relaunch picks it straight back up",
          is_resumable(skipped_step), str(skipped_step.get("done")))
    check("the review of a step that never ran is SKIPPED, not run against nothing",
          entries.get("review:s1", {}).get("outcome") == "SKIPPED",
          str(entries.get("review:s1")))

    # -- when nothing left fits at all -------------------------------------
    where = prep("size-nothing-fits", CLOCK_FIT_SPEC)
    done = run_runner(where, scenario, "--hours", "1", "--only", "s1")
    entries = ledger(where)
    check("when nothing still to run fits, the run STOPS rather than marching on",
          "nothing still to run fits" in done.stdout, done.stdout[-300:])
    check("...and says they are pending, not failed",
          "PENDING, not" in done.stdout, done.stdout[-300:])
    check("...and the step is NOT RUN, which resumes cleanly",
          entries.get("s1", {}).get("outcome") == "NOT RUN", str(entries.get("s1")))


# T3 / F4: a `kind: gate` checkpoint must run the UNIVERSAL gates too, not
# just its own - `run_gate_step` used to run `step.get("gates")` alone, so a
# project whose lint or hygiene check lives in `run.gates` got none of it at
# a checkpoint, despite the README saying it does. Both gates pass (a
# universal gate that FAILS would trip at preflight, before any step, and
# never isolate this from that separate refusal) - the defect is that the
# universal one used to never run at all, which shows up in the LOG, not the
# outcome.
GATE_SPEC = """\
run:
  name: gatestep
  hours: 1
  attempts: 1
  worker_timeout_min: 20
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: universal-marker, cmd: python -c "import sys; sys.exit(0)"}
steps:
  - id: g1
    kind: gate
    title: a checkpoint with its own passing gate
    gates:
      - {name: own-marker, cmd: python -c "import sys; sys.exit(0)"}
"""


def section_26(c):
    root = c.root
    check = c.check

    print("26. a `kind: gate` checkpoint runs the universal gates too")
    where = make_repo(root / "gatestep")
    (where / "overnight" / "steps.yaml").write_text(GATE_SPEC, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "gate spec")
    scen = root / "scenario-gatestep.json"
    scen.write_text("{}", encoding="utf-8")
    done = run_runner(where, scen)
    entry = ledger(where).get("g1", {})
    check("a gate step with its own passing gate is a PASS",
          entry.get("outcome") == "PASS", str(entry))
    gate_log = (where / "overnight" / "runs" / "gatestep" / "g1" / "gates.log").read_text(
        encoding="utf-8")
    check("the step's own gate ran", "own-marker" in gate_log, gate_log[-500:])
    check("...and the universal gate ran too, not just this step's own",
          "universal-marker" in gate_log, gate_log[-500:])
    check("...the step's own gate first, the universal one after",
          gate_log.index("own-marker") < gate_log.index("universal-marker"),
          gate_log[-500:])


# T5 / F5: a REWORK verdict whose one build attempt fails its own gates
# recorded `REVIEW REWORK FAILED` - previously in `RERUN_OUTCOMES` under the
# bare (unreachable) name `REWORK FAILED`, so it was neither resumed nor
# blocking, despite `README.md` documenting it as re-run on relaunch. Paul's
# decision, 2026-09-08: blocking, not resumed - a reviewer flagged the commit
# and a rework failed to repair it, so a person decides before anything
# builds on top of it.
def section_27(c):
    root = c.root
    check = c.check

    print("27. REVIEW REWORK FAILED is blocking, and NOT resumed on relaunch")
    where = make_repo(root / "rework-failed")
    (where / "overnight" / "steps.yaml").write_text(SPEC, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "rework-failed spec", check=False)
    scen = root / "scenario-rework-failed.json"
    scen.write_text(json.dumps({
        "s1": ["pass"], "review:s1": ["review:rework"], "s1#rework": ["regress"],
    }), encoding="utf-8")
    done = run_runner(where, scen, "--only", "s1,review:s1")
    entries = ledger(where)
    check("a rework attempt that fails its own gates is REVIEW REWORK FAILED",
          entries.get("review:s1", {}).get("outcome") == "REVIEW REWORK FAILED",
          str(entries.get("review:s1")))
    test_file = (where / "tests" / "test_s1.py").read_text(encoding="utf-8")
    check("...and the reviewed commit is left standing, not the regression",
          "assert True" in test_file, test_file)
    mode = subprocess.run([sys.executable, str(RUNNER), "--mode", str(where)],
                         capture_output=True, text=True)
    check("--mode reports the plan as BLOCKED - it needs a person",
          mode.stdout.strip().startswith("BLOCKED"), mode.stdout)
    review_step = next(s for s in yaml.safe_load(spec_text(where))["steps"]
                      if s["id"] == "review:s1")
    check("...and it is NOT resumable - a plain relaunch skips it rather than"
          " retrying it",
          not is_resumable(review_step), str(review_step.get("done")))
    check("the run's own exit code is non-zero too",
          done.returncode != 0, str(done.returncode))


# T1: STRIP_ENV keeps a real API key and an inherited effort out of every
# worker - the only untested finding that costs money rather than time (an
# inherited ANTHROPIC_API_KEY bills the API account instead of the
# subscription; an inherited CLAUDE_EFFORT silently overrides the runner's
# own --effort). fake_worker.py now exits 99 if any STRIP_ENV name reaches it,
# so this proves child_env() actually strips them rather than merely
# asserting the runner didn't crash.
def section_28(c):
    root = c.root
    check = c.check

    print("28. STRIP_ENV keeps a real API key and effort out of every worker")
    where = make_repo(root / "stripenv")
    scen = root / "scenario-stripenv.json"
    scen.write_text(json.dumps({"s1": ["pass"]}), encoding="utf-8")
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = "sk-leaked-for-real"
    env["CLAUDE_EFFORT"] = "high"
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = "opus"
    env["OVERNIGHT_FAKE_SCENARIO"] = str(scen)
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-u", str(RUNNER), "--spec", "overnight/steps.yaml",
         "--fake-worker", str(FAKE), "--only", "s1"],
        cwd=where, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    entry = ledger(where).get("s1", {})
    check("a real ANTHROPIC_API_KEY, CLAUDE_EFFORT and"
          " CLAUDE_CODE_SUBAGENT_MODEL in the launching shell never reach the"
          " worker", entry.get("outcome") == "PASS", str(entry))
    check("...the run doesn't merely survive - nothing in the log even hints"
          " at a leak", "STRIP_ENV" not in done.stdout and "child_env" not in done.stdout,
          done.stdout[-300:])


# T2: `on_fail: record` and `on_fail: revert`. Only `on_fail: rework` (the
# default) appeared in any fixture before this - a wrong revert resets the
# operator's branch, and `record` spawning a rework anyway would spend opus
# it was told not to.
def section_29(c):
    root = c.root
    check = c.check

    print("29. on_fail: record and on_fail: revert")

    # -- record: write verdict.json and move on, no rework spawned ----------
    record_spec = SPEC.replace(
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: rework\n",
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: record\n")
    where = make_repo(root / "record")
    (where / "overnight" / "steps.yaml").write_text(record_spec, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "record spec", check=False)
    scen = root / "scenario-record.json"
    scen.write_text(json.dumps({"s1": ["pass"], "review:s1": ["review:rework"]}),
                    encoding="utf-8")
    done = run_runner(where, scen, "--only", "s1,review:s1")
    entries = ledger(where)
    check("on_fail: record leaves the verdict as REVIEW <VERDICT>, not a"
          " rework outcome",
          entries.get("review:s1", {}).get("outcome") == "REVIEW REWORK",
          str(entries.get("review:s1")))
    check("...and the reviewed step itself is untouched - still PASS",
          entries.get("s1", {}).get("outcome") == "PASS", str(entries.get("s1")))
    check("...and no rework worker was ever spawned",
          not (where / "overnight" / "runs" / "selftest" / "s1" / "rework.log").exists())
    check("verdict.json is still written",
          (where / "overnight" / "runs" / "selftest" / "review-s1"
           / "verdict.json").exists())

    # -- revert: reset to the commit before the reviewed one ----------------
    revert_spec = SPEC.replace(
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: rework\n",
        "  - id: review:s1\n    kind: review\n    of: s1\n    on_fail: revert\n")
    where = make_repo(root / "revert")
    (where / "overnight" / "steps.yaml").write_text(revert_spec, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "revert spec", check=False)
    scen = root / "scenario-revert.json"
    scen.write_text(json.dumps({"s1": ["pass"], "review:s1": ["review:fail"]}),
                    encoding="utf-8")
    done = run_runner(where, scen, "--only", "s1,review:s1")
    entries = ledger(where)
    check("on_fail: revert marks the reviewed step REVERTED BY REVIEW",
          entries.get("s1", {}).get("outcome") == "REVERTED BY REVIEW",
          str(entries.get("s1")))
    check("...and the review's own note says it reverted the commit",
          "reverted" in str(entries.get("review:s1", {}).get("note", "")),
          str(entries.get("review:s1")))
    check("...and the reviewed commit's file is actually gone from the tree",
          not (where / "tests" / "test_s1.py").exists())


# T4: `cmd_empty` and `fresh_shell` gate forms. Neither string appeared in any
# fixture before this - a gate that silently passes on output it should fail
# on is worse than no gate at all.
GATES_T4_SPEC = """\
run:
  name: t4gates
  hours: 1
  attempts: 1
  worker_timeout_min: 20
  preamble: overnight/briefs/_preamble.md
steps:
  - id: g1
    kind: gate
    title: cmd_empty and fresh_shell, both passing
    gates:
      - {name: empty-ok, cmd_empty: python -c "pass"}
      - {name: fresh-ok, fresh_shell: python -c "import sys; sys.exit(0)"}
  - id: g2
    kind: gate
    title: cmd_empty that prints something
    gates:
      - {name: empty-bad, cmd_empty: python -c "print(1)"}
  - id: g3
    kind: gate
    title: fresh_shell that exits non-zero
    gates:
      - {name: fresh-bad, fresh_shell: python -c "import sys; sys.exit(1)"}
"""


def section_30(c):
    root = c.root
    check = c.check

    print("30. the cmd_empty and fresh_shell gate forms")
    where = make_repo(root / "t4gates")
    (where / "overnight" / "steps.yaml").write_text(GATES_T4_SPEC, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "t4 gates spec", check=False)
    scen = root / "scenario-t4gates.json"
    scen.write_text("{}", encoding="utf-8")
    done = run_runner(where, scen)
    entries = ledger(where)
    check("cmd_empty passing (exit 0, no output) and fresh_shell passing"
          " (exit 0 in a new shell) both PASS",
          entries.get("g1", {}).get("outcome") == "PASS", str(entries.get("g1")))
    check("cmd_empty exit 0 but with output on stdout FAILS",
          entries.get("g2", {}).get("outcome") == "FAIL"
          and "empty-bad" in str(entries.get("g2", {}).get("note")),
          str(entries.get("g2")))
    check("fresh_shell exit non-zero FAILS",
          entries.get("g3", {}).get("outcome") == "FAIL"
          and "fresh-bad" in str(entries.get("g3", {}).get("note")),
          str(entries.get("g3")))


# T6: `--rerun` re-runs steps already recorded PASS. Untested before this - a
# flag that silently does nothing is the kind of defect nobody notices until
# the morning it was needed.
def section_31(c):
    root = c.root
    check = c.check

    print("31. --rerun re-runs steps already recorded PASS")
    where = make_repo(root / "rerun")
    scen = root / "scenario-rerun.json"
    scen.write_text(json.dumps({"s1": ["pass", "pass"]}), encoding="utf-8")
    run_runner(where, scen, "--only", "s1")
    entries = ledger(where)
    check("the first launch records s1 PASS",
          entries.get("s1", {}).get("outcome") == "PASS", str(entries.get("s1")))
    counters_path = where / "overnight" / "runs" / "selftest" / "_fake_counters.json"
    counters = json.loads(counters_path.read_text(encoding="utf-8"))
    check("...having spawned exactly one worker for it",
          counters.get("s1") == 1, str(counters))

    plain = run_runner(where, scen, "--only", "s1")
    counters = json.loads(counters_path.read_text(encoding="utf-8"))
    check("a plain relaunch of a PASSed step spawns nothing more",
          counters.get("s1") == 1, str(counters))

    done = run_runner(where, scen, "--only", "s1", "--rerun")
    check("--rerun overrides that and the run still succeeds",
          done.returncode == 0, done.stdout[-300:])
    counters = json.loads(counters_path.read_text(encoding="utf-8"))
    check("...spawning a SECOND worker for the already-passed step",
          counters.get("s1") == 2, str(counters))


# Proxy-coverage upgrade: a RUNNING step spans the stop time. The clock is
# checked at the top of the main loop only, so structurally a step already in
# flight cannot be interrupted - but nothing asserted that before this. Also
# proves the flip side: once the stop time has passed, the NEXT step is not
# started. No universal gates here (SPEC's `python -m pytest -q` suite gate
# alone eats seconds of preflight, which swallowed the whole window on the
# first attempt at this fixture) - the expected_min figures are minutes, not
# a claim about the real runtime below, which is what slow-but-alive proves.
SPANS_CLOCK_SPEC = """\
run:
  name: spans-clock
  hours: 1
  attempts: 1
  worker_timeout_min: 20
  isolation: in-place
  preamble: overnight/briefs/_preamble.md
steps:
  - id: s1
    kind: build
    title: legitimately slow, spans the stop time
    brief: overnight/briefs/s1.md
    expected_min: 0.01
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: s2
    kind: build
    title: must not start once the clock has passed
    brief: overnight/briefs/s2.md
    expected_min: 0.01
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
"""


def section_32(c):
    root = c.root
    check = c.check

    print("32. a running step spans the stop time; nothing interrupts it mid-step")
    where = make_repo(root / "spans-clock")
    (where / "overnight" / "steps.yaml").write_text(SPANS_CLOCK_SPEC, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "spans clock spec", check=False)
    scen = root / "scenario-spans-clock.json"
    scen.write_text(json.dumps({"s1": ["slow-but-alive"], "s2": ["pass"]}), encoding="utf-8")
    # slow-but-alive sleeps ~4.8s before passing; the stop time (~3.6s from
    # launch) elapses while s1 is still running.
    done = run_runner(where, scen, "--only", "s1,s2", "--hours", "0.001")
    entries = ledger(where)
    check("a step already running when the stop time passes still finishes and PASSes",
          entries.get("s1", {}).get("outcome") == "PASS", str(entries.get("s1")))
    check("...and the NEXT step is not started once the clock has passed",
          "s2" not in entries, str(entries.get("s2")))
    check("...and the log says why, naming the clock",
          "no step was started after the stop time" in done.stdout, done.stdout[-400:])


# Proxy-coverage upgrade: OVER BUDGET's documented exit code (1) was never
# asserted - only the outcome string was.
def section_33(c):
    root = c.root
    check = c.check

    print("33. OVER BUDGET's exit code is 1, not just its outcome string")
    where = make_repo(root / "budget-exit")
    (where / "overnight" / "steps.yaml").write_text(BUDGET_SPEC, encoding="utf-8")
    sh(where, "git", "add", "-A")
    sh(where, "git", "commit", "-q", "-m", "budget exit spec", check=False)
    scen = root / "scenario-budget-exit.json"
    scen.write_text(json.dumps({"s1": ["over-budget", "pass"]}), encoding="utf-8")
    done = run_runner(where, scen)
    entries = ledger(where)
    check("the step is recorded OVER BUDGET",
          entries.get("s1", {}).get("outcome") == "OVER BUDGET", str(entries.get("s1")))
    check("...and the run's own exit code is 1, as README.md documents",
          done.returncode == 1, str(done.returncode))


# Proxy-coverage upgrade: "a review runs when the clock is tight" was only
# ever exercised via a build that never ran (so its review was SKIPPED, not
# run). `choose()` (overnight.py) never checks a review's `expected_min` -
# it does not have one - against the time left; this calls it directly with
# the stop time an hour in the PAST to prove a review is returned regardless,
# rather than relying on timing a real subprocess.
def section_34(c):
    check = c.check

    print("34. choose() never declines a review for the clock")
    from overnight import Runner
    mock = types.SimpleNamespace(stop_at=time.time() - 3600)
    review_step = {"id": "review:s1", "kind": "review"}
    result = Runner.choose(mock, [review_step], {})
    check("a review is returned even with the stop time an hour in the past",
          result is review_step, str(result))

    build_step = {"id": "s1", "kind": "build", "expected_min": 999999}
    mock2 = types.SimpleNamespace(stop_at=time.time() + 3600, reordered=[], log=lambda *a: None,
                                  record=lambda *a, **k: None, _ran_this_session=set())
    result2 = Runner.choose(mock2, [build_step], {})
    check("...while an oversized BUILD step in the same position is declined",
          result2 is None, str(result2))


SECTIONS = [
    # key   needs        checks  function
    ("1",   (),          70,   section_1_3,
     "preflight, the full scenario, the ledger, resume (1, 2, 2b, 3)"),
    ("4",   (),          8,   section_4,  "a project with no git"),
    ("5",   (),          6,   section_5,  "a third party commits during a step"),
    ("6",   ("1", "5"),  5,   section_6,  "--progress digests a run"),
    ("7",   ("1", "5"),  5,   section_7,  "--mode reads the plan alone"),
    ("8",   (),          4,   section_8,  "the splice itself"),
    ("9",   ("1",),      8,   section_9,  "--reset-state"),
    ("10",  (),          11,   section_10, "--dry-run touches nothing"),
    ("11",  (),          8,   section_11, "a report writes nothing at all"),
    ("12",  (),          7,   section_12, "one runner per repository"),
    ("13",  (),          17,   section_13, "worktree isolation"),
    ("14",  (),          8,   section_14, "isolation is the default"),
    ("15",  (),          33,   section_15, "the usage wall, and the step it cannot see"),
    ("16",  (),          4,   section_16, "a worker's text cannot be printed"),
    ("17",  (),          8,   section_17, "a worktree an earlier run left behind"),
    ("18",  (),          6,   section_18, "work stranded on a scratch branch"),
    ("19",  (),          5,   section_19, "a re-woken worker is counted once"),
    ("20",  (),          9,   section_20, "a stalled worker is killed"),
    ("21",  (),          14,   section_21, "the per-step budget"),
    ("22",  (),          17,   section_22, "a cut-off worker is continued"),
    ("23",  (),          12,   section_23, "stranded work is retested, not discarded"),
    ("24",  (),          20,   section_24, "the clock: --until, and the stop it enforces"),
    ("25",  (),          30,   section_25, "expected_min is required, and it schedules"),
    ("26",  (),          4,   section_26, "a `kind: gate` checkpoint runs the universal gates"),
    ("27",  (),          5,   section_27, "REVIEW REWORK FAILED is blocking, not resumed"),
    ("28",  (),          2,   section_28, "STRIP_ENV keeps a leaked key and effort out"),
    ("29",  (),          7,   section_29, "on_fail: record and on_fail: revert"),
    ("30",  (),          3,   section_30, "the cmd_empty and fresh_shell gate forms"),
    ("31",  (),          5,   section_31, "--rerun re-runs steps already recorded PASS"),
    ("32",  (),          3,   section_32, "a running step spans the stop time"),
    ("33",  (),          2,   section_33, "OVER BUDGET's exit code"),
    ("34",  (),          2,   section_34, "choose() never declines a review for the clock"),
]

TOTAL_CHECKS = sum(s[2] for s in SECTIONS)

# 1, 2, 2b and 3 share one repository and run in sequence - 1 dirties the tree, 2
# runs the full scenario, 2b reads the ledger it wrote, 3 resumes it. Naming any
# of them selects the block, because none of them means anything without the rest.
ALIASES = {"2": "1", "2b": "1", "3": "1", "1-3": "1", "1-2-2b-3": "1"}

ORDER = {key: i for i, (key, *_) in enumerate(SECTIONS)}


def resolve(keys):
    """The sections to run, in suite order, with prerequisites pulled in.

    Returns (to run, what was pulled in). A caller asking for 6 gets 1 and 5 as
    well, and is told so - the alternative is running section 6 against a
    repository no run has touched, which passes or fails for reasons that have
    nothing to do with `--progress`.
    """
    unknown = [k for k in keys if ALIASES.get(k, k) not in ORDER]
    if unknown:
        raise SystemExit(f"selftest: no such section: {', '.join(unknown)}\n"
                         f"          try --list")
    want = {ALIASES.get(k, k) for k in keys}
    asked = set(want)
    grew = True
    while grew:
        grew = False
        for key, needs, *_ in SECTIONS:
            if key in want:
                for n in needs:
                    if n not in want:
                        want.add(n)
                        grew = True
    chosen = [s for s in SECTIONS if s[0] in want]
    return chosen, sorted(want - asked, key=ORDER.__getitem__)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="selftest.py",
        description="Exercise every path of overnight.py against a fake worker.",
        epilog="With no arguments it runs the whole suite, which is the only form "
               "that prints SELFTEST PASS and the only form that may precede a launch.")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--only", metavar="N[,N...]",
                       help="run just these sections, and whatever they need")
    which.add_argument("--from", dest="from_", metavar="N",
                       help="run from this section to the end of the suite")
    ap.add_argument("--list", action="store_true",
                    help="print the sections and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="no progress heartbeat on the full suite (a partial run "
                         "never has one), so the output matches what it printed "
                         "before the heartbeat existed")
    args = ap.parse_args(argv)

    if args.list:
        print(f"{'':<5} {'needs':<8} {'checks':>6}  section")
        for key, needs, checks, _, title in SECTIONS:
            print(f"{key:<5} {','.join(needs) or '-':<8} {checks:>6}  {title}")
        print(f"\n{len(SECTIONS)} sections, {TOTAL_CHECKS} checks.")
        return 0

    if args.only:
        chosen, pulled = resolve([k.strip() for k in args.only.split(",") if k.strip()])
    elif args.from_:
        start = ALIASES.get(args.from_, args.from_)
        if start not in ORDER:
            raise SystemExit(f"selftest: no such section: {args.from_}\n"
                             f"          try --list")
        chosen, pulled = resolve([key for key, *_ in SECTIONS[ORDER[start]:]])
    else:
        chosen, pulled = list(SECTIONS), []

    partial = len(chosen) != len(SECTIONS)
    if partial:
        print(f"running {len(chosen)} of {len(SECTIONS)} sections: "
              f"{', '.join(s[0] for s in chosen)}")
        if pulled:
            print(f"  including {', '.join(pulled)}, which the sections you asked "
                  f"for cannot stand without")
        print()

    root = Path(tempfile.mkdtemp(prefix="overnight-selftest-"))
    c = Ctx(root)
    if not partial and not args.quiet:
        c.start_heartbeat(TOTAL_CHECKS)
    spent = []
    try:
        for key, _needs, expected, fn, _title in chosen:
            before, started = c.checks, time.time()
            c.section = key
            fn(c)
            ran, took = c.checks - before, time.time() - started
            spent.append((key, ran, took))
            if ran != expected:
                # Loud, because the alternative is a suite that quietly shrank.
                name = f"section {key} made {ran} checks, not the {expected} expected"
                print(f"  FAIL {name}"
                      f"  (a check was lost, or SECTIONS wants {ran} here)")
                c.failures.append(name)
        if not partial and c.checks != TOTAL_CHECKS:
            name = f"the suite made {c.checks} checks, not the {TOTAL_CHECKS} expected"
            print(f"  FAIL {name}")
            c.failures.append(name)
        if c.failures and c.shared.get("log"):
            print("\n--- run.log tail ---")
            print(c.shared["log"][-4000:])
    finally:
        c.stop_heartbeat()
        if c.failures and os.environ.get("OVERNIGHT_KEEP"):
            print(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    # Where the time went, dearest first. This is the table that makes `--only`
    # usable: the suite is minutes, not seconds, and it is worth knowing which
    # sections are buying that with real waiting and which are nearly free.
    if len(spent) > 1:
        print("\nwhere the time went")
        for key, ran, took in sorted(spent, key=lambda s: -s[2]):
            print(f"  {key:<4} {ran:>3} checks  {took / 60:>5.1f} min")
        print(f"  {'':<4} {c.checks:>3} checks  "
              f"{sum(s[2] for s in spent) / 60:>5.1f} min in total")

    # A partial run NEVER prints the token a launch is gated on. `SELFTEST PASS`
    # means the whole suite passed and nothing else; every other outcome says what
    # it really was.
    if c.failures:
        verdict = ("SELFTEST PARTIAL FAIL: " if partial else "SELFTEST FAIL: ") \
            + ", ".join(c.failures)
    elif partial:
        verdict = (f"SELFTEST PARTIAL OK - sections {', '.join(s[0] for s in chosen)}"
                   f", {c.checks} checks. NOT the full suite; run it before you commit.")
    else:
        verdict = f"SELFTEST PASS ({c.checks} checks)"
    print(f"\n{verdict}")
    return 0 if not c.failures else 1


if __name__ == "__main__":
    sys.exit(main())
