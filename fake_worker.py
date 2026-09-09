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
"""fake_worker.py - stands in for `claude` so the runner's every path can be
exercised in seconds without spending a token.

The runner invokes it exactly as it would invoke claude (same argv after the
binary), with the brief on stdin and these environment variables:
OVERNIGHT_STEP_ID, OVERNIGHT_STEP_KIND, OVERNIGHT_REPO, OVERNIGHT_OUT,
OVERNIGHT_SPEC. It reads a SCENARIO - a JSON file named by
OVERNIGHT_FAKE_SCENARIO mapping a step key to the list of behaviours to perform
on successive calls - and emits stream-json the way claude does, ending with a
`result` event that carries `structured_output` where a schema was asked for.

Step keys: the step id; `<id>#rework` for a rework call (the brief carries the
REWORK heading); a review or reflect step by its own id.

Behaviours:
  pass          write tests/test_<id>.py (passing) and commit
  fail-notest   do nothing
  wall          THE USAGE WALL: exit 1 having emitted no result event at all,
                the way `claude -p` does when the account's window is exhausted.
                The key for a park probe is `_probe`, so a scenario can say
                ["wall", "wall", "probe-ok"] to make the account come back.
  probe-ok      answer a park probe normally (the default for kind `probe`)
  stall         WEDGED: alive, emitting nothing at all, forever - what a hung
                PreToolUse hook does to a worker. The stall watchdog must kill it
  slow-but-alive  legitimately slow: silent work, but a tool_progress heartbeat
                every so often, so the log keeps growing. Must NOT be killed
  pass-rewoken  pass, but emit TWO result events the way a worker does when a
                backgrounded task wakes it after its first session ended: turns
                are per-session and must be summed, cost is cumulative over the
                process and must not be
  pass-unicode  pass, but report a summary the console may not be able to print
                (a `<=`, an accent, a tick), which is what a real worker writes
                and what killed a live run under Task Scheduler's cp1252 console
  over-budget   the per-step `--max-budget-usd` cap trips: the worker is cut off
                part-way, having written but not committed, and exits 1 with a
                result event of subtype error_max_budget_usd
  over-budget-late  the same trip, but AFTER the work is committed and the gates
                will pass - a step that succeeded and merely ran out on the way
                out, which must still be a PASS
  over-budget-idle  the RUNAWAY: the cap is spent and the tree is byte-identical.
                There is nothing to hand on, so it must never be continued
  finish-the-handover  a CONTINUATION worker. It fails unless the previous
                worker's half-built file is still in the tree AND the brief says
                it is a continuation, so it proves the handover really happened
  commit-wrong  commit real work but not the test the gate demands
  regress       overwrite an already-passing test with a failing one and commit
                it - the only way a REWORK attempt on top of a passing step
                fails its own gate, since the gate runs against the tree as it
                stands and the original passing commit is still there
  foreign-commit  a THIRD PARTY commits to the branch during the step, gate fails
  pass+main:unrelated   the worker passes in its own tree while a third party
                commits something unrelated to the operator's branch
  pass+main:conflict    the same, but the third party writes the SAME file the
                worker is writing, so the work cannot be replayed
  fail-dirty    write the test file and leave it uncommitted
  diag          write <out>/<id>/remediation.md
  review:pass | review:rework | review:fail
                add `+stray` (e.g. review:rework+stray) to leave an untracked
                file behind, which the reset after the review must quarantine
  reflect:none  change nothing
  reflect:add   append a build step `added-by-<id>` with a brief file
  reflect:tamper  change the brief path of the first COMPLETED step
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(text, structured=None):
    payload = {"type": "result", "subtype": "success", "is_error": False,
               "num_turns": 1, "total_cost_usd": 0.01, "result": text}
    if structured is not None:
        payload["structured_output"] = structured
        payload["result"] = json.dumps(structured)
    emit(payload)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True)


def git_as_stranger(repo, *args):
    """A commit by somebody who is NOT this run: the operator, another window, a hook.

    The runner stamps every worker commit with a run-specific GIT_COMMITTER_EMAIL,
    inherited through this process. Dropping it - and naming somebody else - is what
    a third party writing to the branch mid-step actually looks like.
    """
    env = dict(os.environ)
    for key in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        env.pop(key, None)
    env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"] = "someone else"
    env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"] = "stranger@example.invalid"
    subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, env=env)


def main():
    brief = sys.stdin.read()
    step = os.environ.get("OVERNIGHT_STEP_ID", "?")
    kind = os.environ.get("OVERNIGHT_STEP_KIND", "build")
    repo = Path(os.environ["OVERNIGHT_REPO"])
    out = Path(os.environ["OVERNIGHT_OUT"])
    scenario_path = os.environ.get("OVERNIGHT_FAKE_SCENARIO")
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8")) if scenario_path else {}
    key = step + ("#rework" if "# REWORK:" in brief else "")
    if kind == "diagnostic":
        key = step + "#diag"
    counter_path = out / "_fake_counters.json"
    counters = json.loads(counter_path.read_text()) if counter_path.exists() else {}
    n = counters.get(key, 0)
    counters[key] = n + 1
    counter_path.write_text(json.dumps(counters))
    # The default must match the STEP KIND. A review or reflect falling back to the
    # build behaviour writes a file and commits it, which a real read-only reviewer
    # cannot do: the run then shows INCONCLUSIVE verdicts and a reflect whose commit
    # gets discarded, and the demonstration teaches the wrong thing.
    default = {"diagnostic": "diag", "review": "review:pass",
               "reflect": "reflect:none", "probe": "probe-ok"}.get(kind, "pass")
    behaviours = scenario.get(key) or [default]
    behaviour = behaviours[min(n, len(behaviours) - 1)]

    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"fake worker: step {key}, call {n + 1}, behaviour {behaviour}"}]}})

    if behaviour == "wall":
        # NO RESULT EVENT, and a non-zero exit. That combination is the whole
        # signal the runner watches for, and it is what the real CLI does when the
        # five-hour window closes: it says so in prose and dies, having done no
        # work and reported no cost.
        sys.stdout.write("You've hit your session limit \u00b7 resets 12:40am"
                         " (Europe/London)\n")
        sys.stdout.flush()
        sys.exit(1)
    if behaviour == "probe-ok":
        return result("ok")
    safe = re.sub(r"[^a-z0-9_]", "_", step.lower())
    # `pass+main:...` - the worker does its normal job in its own tree while a
    # THIRD PARTY commits to the operator's branch, which is what isolation exists
    # to survive. Under isolation OVERNIGHT_REPO is the worktree, so the stranger
    # has to be told to write to OVERNIGHT_MAIN_REPO explicitly.
    main_meddle = ""
    if behaviour.startswith("pass+main:"):
        behaviour, main_meddle = "pass", behaviour.split(":", 1)[1]
    if main_meddle:
        main = Path(os.environ.get("OVERNIGHT_MAIN_REPO") or repo)
        if main_meddle == "stray":
            # THE FIELD INCIDENT of 2026-09-06, exactly: no commit, no git at all -
            # just a file appearing in the operator's tree while the step runs,
            # after the step's untracked snapshot was taken. It was the operator's
            # own session log. In-place it fails the worker's clean_tree gate and
            # costs the whole attempt; isolated it is nothing to do with the worker.
            (main / "Session history").mkdir(exist_ok=True)
            (main / "Session history" / "notes.md").write_text(
                "The operator's own file, written mid-step.\n", encoding="utf-8")
        elif main_meddle == "unrelated":
            (main / "someone_elses_work.md").write_text("Not the worker's.\n",
                                                        encoding="utf-8")
            git_as_stranger(main, "add", "someone_elses_work.md")
        else:
            # The SAME file the worker is writing, with different content: the
            # work cannot be replayed onto the branch without a person.
            (main / "tests").mkdir(exist_ok=True)
            (main / "tests" / f"test_{safe}.py").write_text(
                f"def test_{safe}():\n    assert True  # theirs, not the worker's\n",
                encoding="utf-8")
            git_as_stranger(main, "add", "-A")
        if main_meddle != "stray":
            git_as_stranger(main, "commit", "-q", "-m", "a commit from another window")
    if behaviour == "stall":
        # WEDGED: alive, writing nothing, forever. What a worker looks like when a
        # PreToolUse hook never returns - the tool call never completes, so no
        # tool_progress heartbeat and no thinking_tokens record is ever emitted.
        # Twice on 2026-09-07 this burned the full 90-minute worker_timeout_min.
        time.sleep(3600)
        return result("never reached")
    if behaviour == "slow-but-alive":
        # THE CASE THE WATCHDOG MUST NOT KILL: a legitimately slow step. It writes
        # nothing a human would call output, but Claude Code emits a tool_progress
        # heartbeat about every 30 seconds while a Bash call runs, and that is what
        # keeps the log growing. Same shape here: silent work, ticking log.
        for elapsed in range(10, 130, 10):
            emit({"type": "tool_progress", "tool_name": "Bash", "heartbeat": True,
                  "elapsed_time_seconds": elapsed})
            time.sleep(0.4)
        # Then it does the work and commits, like any passing worker. The point of
        # the case is that a slow step SUCCEEDS on its first attempt - if it fell
        # through to a gate failure the retry would mask a watchdog that had
        # wrongly killed it.
        behaviour = "pass"
    if behaviour == "pass-rewoken":
        # A worker that BACKGROUNDS a task: its `result` fires, the task then
        # finishes and wakes the process, and a second session is appended to the
        # same stream. One process, one log, TWO results - and their fields do not
        # accumulate alike. The numbers below are the real ones measured off
        # FinKit `5b-recognise` attempt-2 on 2026-09-07: per-session turns
        # (119 then 4) but a CUMULATIVE cost (13.99 then 14.53, not 0.54).
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / f"test_{safe}.py"
        path.write_text(f"def test_{safe}():\n    assert True  # built\n", encoding="utf-8")
        git(repo, "add", str(path.relative_to(repo).as_posix()))
        git(repo, "commit", "-q", "-m", f"fake: {key} built")
        # One assistant record per session, because a real log carries one per
        # streamed API call and tally.py identifies a call by its message id.
        # Their usage is a single call's worth; the result events carry the
        # session totals, which is exactly how a real worker's log is shaped.
        emit({"type": "assistant", "message": {
            "id": "msg_session_1", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "first session"}],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1744,
                      "cache_read_input_tokens": 154157, "output_tokens": 920}}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "num_turns": 119, "total_cost_usd": 13.9880725, "model": "claude-opus-5",
              "duration_ms": 2807387, "result": f"{key}: first session",
              "usage": {"input_tokens": 230, "cache_creation_input_tokens": 207519,
                        "cache_read_input_tokens": 18344715, "output_tokens": 109575}})
        emit({"type": "system", "subtype": "init", "session_id": "same-process"})
        emit({"type": "assistant", "message": {
            "id": "msg_session_2", "model": "claude-opus-5",
            "content": [{"type": "text", "text": "woken"}],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 898,
                      "cache_read_input_tokens": 222336, "output_tokens": 615}}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "num_turns": 4, "total_cost_usd": 14.5301945, "model": "claude-opus-5",
              "duration_ms": 203050, "result": f"{key}: woken by a background task",
              "usage": {"input_tokens": 8, "cache_creation_input_tokens": 3591,
                        "cache_read_input_tokens": 889344, "output_tokens": 2460}})
        return
    if behaviour == "over-budget-idle":
        # THE RUNAWAY. The cap is spent and the tree is byte-identical: the worker
        # went nowhere, confidently, for the whole budget. There is nothing to hand
        # to a continuation, and handing one a half-built wrong thing to finish is
        # worse than stopping - so this must NOT be continued however many
        # continuations the run allows.
        emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
              "terminal_reason": "budget_exhausted", "num_turns": 44,
              "duration_ms": 1500000, "total_cost_usd": 6.0004,
              "result": f"{key}: spent the cap and wrote nothing"})
        sys.exit(1)
    if behaviour == "finish-the-handover":
        # THE CONTINUATION. It refuses to do anything unless the previous worker's
        # half-finished file is actually in front of it, which is what proves the
        # tree was carried forward rather than reset - the single thing that makes
        # a continuation cheaper than a retry. It also checks it was TOLD it is a
        # continuation, because a worker that is not told will start over.
        half = repo / "tests" / f"test_{safe}.py"
        if not half.exists():
            emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True,
                 "content": "Error: nothing was handed over - the tree was reset"}]}})
            return result(f"{key}: nothing to continue from")
        if "# CONTINUATION" not in brief:
            emit({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": True,
                 "content": "Error: no handover in the brief"}]}})
            return result(f"{key}: not told it was a continuation")
        half.write_text(f"def test_{safe}():\n    assert True  # finished by the"
                        " continuation\n", encoding="utf-8")
        git(repo, "add", str(half.relative_to(repo).as_posix()))
        git(repo, "commit", "-q", "-m", f"fake: {key} finished after a handover")
        return result(f"{key}: picked up the half-built work and finished it")
    if behaviour in ("over-budget", "over-budget-late"):
        # THE PER-STEP CAP TRIPPED. `--max-budget-usd` cuts the worker off at a
        # turn boundary, part-way through the job: here the test file is written
        # but nothing is committed, so the gate fails - and a retry would only
        # spend a fresh cap to be cut off at the same place. Shape measured
        # against the real CLI on 2026-09-07: exit 1, and a result event whose
        # subtype is error_max_budget_usd, is_error true, terminal_reason
        # budget_exhausted. It HAS a result event and a cost, so the wall
        # counter must read it as a worker that answered, not a barren one.
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / f"test_{safe}.py"
        path.write_text(f"def test_{safe}():\n    assert True  # half done\n",
                        encoding="utf-8")
        if behaviour == "over-budget-late":
            # THE OTHER HALF: the cap trips on the tidying up, AFTER the work is
            # committed and the gates will pass. The step succeeded; that it ran
            # out of money on the way out is nobody's business, and calling it
            # OVER BUDGET would throw away work that is on the branch.
            git(repo, "add", str(path.relative_to(repo).as_posix()))
            git(repo, "commit", "-q", "-m", f"fake: {key} built, then ran out")
        emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
              "terminal_reason": "budget_exhausted", "num_turns": 37,
              "duration_ms": 1200000, "total_cost_usd": 6.0231,
              "result": f"{key}: cut off by the budget part-way through"})
        sys.exit(1)
    if behaviour in ("pass", "fail-dirty", "pass-unicode"):
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / f"test_{safe}.py"
        marker = "reworked" if key.endswith("#rework") else "built"
        path.write_text(f"def test_{safe}():\n    assert True  # {marker}\n", encoding="utf-8")
        if behaviour in ("pass", "pass-unicode"):
            # BY EXPLICIT PATH, which is what a real worker's brief tells it to do.
            # With `add -A` this fixture swept up whatever else was in the tree -
            # including a file the operator wrote mid-step, the exact defect of
            # 2026-09-05 - and so could not reproduce the defect of 2026-09-06
            # either, because the stray got committed instead of failing the gate.
            git(repo, "add", str(path.relative_to(repo).as_posix()))
            git(repo, "commit", "-q", "-m", f"fake: {key} {marker}")
        if behaviour == "pass-unicode":
            # ESCAPES, not literals: this repository is ASCII-only, and the
            # characters that break a cp1252 console are exactly the ones
            # that render as mojibake in a diff. The runner sees the real
            # characters; the source stays readable everywhere.
            return result("deflection \u2264 2.5 mm over a 900 \u00d7 400"
                          " panel; caf\u00e9 shelf checked \u2713")
        return result(f"{key}: {behaviour}")
    if behaviour == "commit-wrong":
        # Commits real work, then fails the gate anyway - the case where a reset
        # discards something worth keeping.
        (repo / f"{safe}_notes.md").write_text("Work worth keeping.\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"fake: {key} did work but missed the contract")
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "Error: pretend failure"}]}})
        return result(f"{key}: committed, no test")
    if behaviour == "foreign-commit":
        # A third party commits to the branch DURING the step, and the step then
        # fails its gate. Resetting to the step baseline would take the stranger's
        # commit with it, silently.
        (repo / "someone_elses_work.md").write_text("Not the worker's.\n", encoding="utf-8")
        git_as_stranger(repo, "add", "someone_elses_work.md")
        git_as_stranger(repo, "commit", "-q", "-m", "a commit from another window")
        return result(f"{key}: a stranger committed; no test written")
    if behaviour == "fail-notest":
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "Error: pretend failure"}]}})
        return result(f"{key}: did nothing")
    if behaviour == "regress":
        # A REWORK attempt that makes things WORSE: the step already has a
        # passing commit (gates run against the tree as it stands, not a
        # fresh one), so failing to add anything - or committing something
        # extra alongside it, as `commit-wrong` does - still leaves the
        # original test passing. This overwrites it with a failing one and
        # commits that, which is the only way a rework attempt on top of an
        # already-passing step actually fails its own gate.
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / f"test_{safe}.py"
        path.write_text(f"def test_{safe}():\n    assert False  # regressed by rework\n",
                        encoding="utf-8")
        git(repo, "add", str(path.relative_to(repo).as_posix()))
        git(repo, "commit", "-q", "-m", f"fake: {key} regressed the passing test")
        return result(f"{key}: committed a regression")
    if behaviour == "diag":
        (out / step).mkdir(parents=True, exist_ok=True)
        (out / step / "remediation.md").write_text("# remediation\n\nDo the thing properly.\n",
                                                   encoding="utf-8")
        return result("remediation written")
    if behaviour.startswith("review:"):
        verdict = behaviour.split(":", 1)[1]
        if verdict.endswith("+stray"):
            # A reviewer is read-only, and the runner resets after it anyway - but
            # `+stray` leaves an untracked file behind, the way a person or a tool
            # leaves a scratch note in a repository that is not frozen for the hours
            # a run lasts. That reset must QUARANTINE it, not delete it.
            verdict = verdict[:-len("+stray")]
            (repo / "review-scratch.txt").write_text(
                "a note somebody was in the middle of\n", encoding="utf-8")
        findings = [] if verdict == "pass" else [
            {"severity": "major", "where": "tests/", "what": "test asserts True",
             "fix": "assert something real"}]
        return result("review done", {"verdict": verdict, "summary": f"fake {verdict}",
                                      "findings": findings,
                                      "rework_brief": "Make the test mean something."})
    if behaviour.startswith("reflect:"):
        action = behaviour.split(":", 1)[1]
        spec_path = Path(os.environ["OVERNIGHT_SPEC"])
        text = spec_path.read_text(encoding="utf-8")
        added, removed = [], []
        if action == "add":
            new_id = f"added-by-{step}"
            brief_dir = spec_path.parent / "briefs"
            brief_dir.mkdir(exist_ok=True)
            (brief_dir / f"{new_id}.md").write_text("Build the added thing.\n", encoding="utf-8")
            # `expected_min` because a build step without one no longer loads, and
            # a reflect step rewrites the plan the runner is about to re-read. A
            # real reflect worker is told the same thing by its brief.
            text += (f"  - id: {new_id}\n    kind: build\n    title: added by reflect\n"
                     f"    expected_min: 12\n"
                     f"    brief: {(brief_dir / (new_id + '.md')).relative_to(repo).as_posix()}\n"
                     f"    gates:\n      - cmd: python -m pytest -q tests/test_{re.sub(r'[^a-z0-9_]', '_', new_id)}.py\n")
            spec_path.write_text(text, encoding="utf-8")
            added = [new_id]
        elif action == "tamper":
            # The spec IS the ledger now: a step that has run is one carrying
            # `done:`. There is no state.json to consult.
            import yaml                                    # only the tamper path needs it
            spec = yaml.safe_load(text)
            ran = [s["id"] for s in spec["steps"]
                   if s.get("done") and s.get("kind", "build") == "build"]
            if ran:
                text = text.replace(f"briefs/{ran[0]}.md", f"briefs/{ran[0]}-TAMPERED.md", 1)
                spec_path.write_text(text, encoding="utf-8")
        return result("reflected", {"changed": action != "none",
                                    "rationale": f"fake reflect {action}",
                                    "added": added, "removed": removed})
    return result(f"unknown behaviour {behaviour}")


if __name__ == "__main__":
    main()
