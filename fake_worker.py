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
  commit-wrong  commit real work but not the test the gate demands
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
               "reflect": "reflect:none"}.get(kind, "pass")
    behaviours = scenario.get(key) or [default]
    behaviour = behaviours[min(n, len(behaviours) - 1)]

    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"fake worker: step {key}, call {n + 1}, behaviour {behaviour}"}]}})

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
    if behaviour in ("pass", "fail-dirty"):
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        path = tests / f"test_{safe}.py"
        marker = "reworked" if key.endswith("#rework") else "built"
        path.write_text(f"def test_{safe}():\n    assert True  # {marker}\n", encoding="utf-8")
        if behaviour == "pass":
            # BY EXPLICIT PATH, which is what a real worker's brief tells it to do.
            # With `add -A` this fixture swept up whatever else was in the tree -
            # including a file the operator wrote mid-step, the exact defect of
            # 2026-09-05 - and so could not reproduce the defect of 2026-09-06
            # either, because the stray got committed instead of failing the gate.
            git(repo, "add", str(path.relative_to(repo).as_posix()))
            git(repo, "commit", "-q", "-m", f"fake: {key} {marker}")
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
            text += (f"  - id: {new_id}\n    kind: build\n    title: added by reflect\n"
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
