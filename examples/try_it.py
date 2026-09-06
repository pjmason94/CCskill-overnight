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
"""try_it.py - run the example plan end to end, for no tokens.

    python examples/try_it.py            # run it, then delete the scratch repo
    python examples/try_it.py --keep     # leave the scratch repo to poke at

Builds a throwaway git repository, copies the example plan and briefs into it at
the convention's location (`overnight/steps.yaml`, `overnight/briefs/`), and runs
the real runner against `fake_worker.py` instead of `claude`. Nothing is spent
and nothing outside the scratch directory is touched.

It is here so a new project can watch the loop work - a build, a review that asks
for rework, the rework, a reflect - before anybody writes a real brief. The
project it builds is invented; see steps.example.yaml.

Needs `git` and `pytest`.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent

# What the stand-in worker does at each step, so the demonstration shows the paths
# that matter rather than six easy passes: a review that asks for rework and the
# rework that follows, and a build that fails its gate once and passes on retry.
SCENARIO = {
    "parse-durations": ["pass"],
    "review:parse-durations": ["review:rework"],
    "parse-durations#rework": ["pass"],
    "humanise": ["fail-notest", "pass"],
    "reflect-1": ["reflect:none"],
    "cli": ["pass"],
    "review:cli": ["review:pass"],
}


def sh(cwd, *argv):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the scratch repository afterwards")
    args = parser.parse_args()

    if not shutil.which("git"):
        print("git is not on PATH. The example needs it: the runner's undo is git,"
              " and without it this demonstration would show a degraded run.")
        return 2

    root = Path(tempfile.mkdtemp(prefix="overnight-example-"))
    repo = root / "tickerbell"
    try:
        (repo / "overnight").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "src" / "tickerbell").mkdir(parents=True)

        # A base test so the universal `pytest -q` gate has something to collect:
        # pytest exits non-zero when it collects nothing, which would fail the
        # preflight before any worker ran.
        (repo / "tests" / "test_base.py").write_text(
            "def test_the_suite_runs():\n    assert True\n", encoding="utf-8")
        (repo / "src" / "tickerbell" / "__init__.py").write_text(
            '"""An invented library. See examples/steps.example.yaml."""\n',
            encoding="utf-8")
        (repo / ".gitignore").write_text(
            "overnight/runs/\novernight/DECISIONS-PENDING.md\n"
            "__pycache__/\n.pytest_cache/\n", encoding="utf-8")

        shutil.copyfile(HERE / "steps.example.yaml", repo / "overnight" / "steps.yaml")
        shutil.copytree(HERE / "briefs", repo / "overnight" / "briefs")

        sh(repo, "git", "init", "-q")
        sh(repo, "git", "config", "user.email", "example@invalid")
        sh(repo, "git", "config", "user.name", "overnight example")
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", "the invented project, before the run")

        scenario = root / "scenario.json"
        scenario.write_text(json.dumps(SCENARIO), encoding="utf-8")
        env = dict(os.environ)
        env["OVERNIGHT_FAKE_SCENARIO"] = str(scenario)
        env["PYTHONIOENCODING"] = "utf-8"

        print(f"scratch repository: {repo}\n", flush=True)
        done = subprocess.run(
            [sys.executable, "-u", str(SKILL / "overnight.py"),
             "--spec", "overnight/steps.yaml",
             "--fake-worker", str(SKILL / "fake_worker.py")],
            cwd=repo, env=env)

        print(f"\nrunner exit {done.returncode}", flush=True)
        print("\n--- what it wrote ---", flush=True)
        subprocess.run([sys.executable, "-u", str(SKILL / "overnight.py"), "--progress"],
                       cwd=repo)
        summary = repo / "overnight" / "runs" / "example" / "SUMMARY.md"
        if summary.exists():
            print(f"\n--- {summary.name} ---")
            print(summary.read_text(encoding="utf-8"))
        if args.keep:
            print(f"\nkept: {root}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
