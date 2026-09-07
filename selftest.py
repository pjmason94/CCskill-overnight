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
throwaway git repository, in under a minute and for no tokens.

    python selftest.py

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
import contextlib
import io
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "overnight.py"
FAKE = HERE / "fake_worker.py"

sys.path.insert(0, str(HERE))
from overnight import Log, _step_blocks, is_resumable, upsert_done   # noqa: E402


def _blocks_of(text):
    lines = text.splitlines(keepends=True)
    return {sid: "".join(lines[a:b]) for sid, a, b, _ in _step_blocks(text)}

SPEC = """\
run:
  name: selftest
  hours: 1
  attempts: 3
  worker_timeout_min: 2
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
    kind: build
    title: fails twice then passes
    brief: overnight/briefs/s2.md
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
  - id: reflect-1
    kind: reflect
  - id: s3
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
  worker_timeout_min: 2
  isolation: worktree
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
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
  worker_timeout_min: 2
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    kind: build
    title: the operator writes a file into their own tree mid-step
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
"""

FOREIGN_SPEC = """\
run:
  name: foreign
  hours: 1
  attempts: 1
  worker_timeout_min: 2
  isolation: in-place              # the refusal being tested is an in-place defence
  preamble: overnight/briefs/_preamble.md
  gates:
    - {clean_tree: true}
steps:
  - id: s1
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
  worker_timeout_min: 2
  preamble: overnight/briefs/_preamble.md
  gates:
    - {name: suite, cmd: python -m pytest -q tests/test_base.py}
    - {clean_tree: true}
steps:
  - id: s1
    kind: build
    title: the account's window closes during this step
    brief: overnight/briefs/s1.md
    gates:
      - cmd: python -m pytest -q tests/test_s1.py
  - id: s2
    kind: build
    title: must never be started while the wall stands
    brief: overnight/briefs/s2.md
    gates:
      - cmd: python -m pytest -q tests/test_s2.py
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


def main():
    root = Path(tempfile.mkdtemp(prefix="overnight-selftest-"))
    failures = []

    def check(name, condition, detail=""):
        print(f"  {'ok  ' if condition else 'FAIL'} {name}" + (f"  ({detail})" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    try:
        repo = make_repo(root)
        scenario_path = root / "scenario.json"
        scenario_path.write_text(json.dumps(SCENARIO), encoding="utf-8")

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
        check("the ledger records the tier the runner asked for",
              steps.get("s1", {}).get("tier") == "opus/medium",
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

        print("7. --mode decides which way /overnight goes, from the plan alone")

        def mode(where):
            done_ = subprocess.run([sys.executable, "-u", str(RUNNER), "--mode"],
                                   cwd=where, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
            return done_.stdout.splitlines()[0] if done_.stdout else "", done_.returncode

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

        print("8. the splice itself")
        # A hand edit to a PENDING step while the run is live must survive the next
        # record: the runner re-reads the file at record time rather than writing
        # back a copy taken at step start.
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

        print("15. the usage wall: park or stop, but never march through the plan")
        # THE INCIDENT OF 2026-09-07, reproduced. The account's five-hour window
        # closed mid-run; every worker after it returned nothing in seconds, and
        # the runner - unable to tell "this step failed" from "nothing can succeed"
        # - retried, diagnosed, marked STUCK and moved on, thirty-odd times. The
        # morning showed a plan whose every step needed re-running and a summary
        # full of findings about code no worker had ever read.

        def wall_repo(name, scenario, *extra):
            where = make_repo(root / name)
            (where / "overnight" / "steps.yaml").write_text(WALL_SPEC, encoding="utf-8")
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

        if failures:
            print("\n--- run.log tail ---")
            print(log[-4000:])
    finally:
        if failures and os.environ.get("OVERNIGHT_KEEP"):
            print(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    print(f"\n{'SELFTEST PASS' if not failures else 'SELFTEST FAIL: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
