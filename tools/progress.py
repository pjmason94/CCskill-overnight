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
"""progress.py - one line per run, for somebody who asked at 3am.

    python tools/progress.py <project dir> [<project dir> ...]

Prints exactly one line per project and nothing else: whether a run is live, what
it is doing this minute, how far through the plan it is, and what it has spent.

It is READ-ONLY and touches nothing a live run owns - it reads the lock, the run
log and the plan, and never writes, resets or locks anything itself. Safe to run
against a run in flight, which is the only time anybody wants it.

Liveness comes from the run's own `.lock` (pid + host + run name), so this needs
no process table and no arguments beyond the project. A lock whose pid is dead is
reported as a finished run, not a live one.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OUT = "overnight/runs"                       # DEFAULT_OUT in overnight.py
DONE = re.compile(r"^\s*outcome:\s*(.+?)\s*$", re.M)
COST = re.compile(r"^\s*cost_usd:\s*([0-9.]+)\s*$", re.M)
STEP = re.compile(r"^\s*- id:\s*(.+?)\s*$", re.M)
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")


def pid_alive(pid):
    """True if that pid is running. Mirrors overnight.py: on Windows os.kill(pid,
    0) TERMINATES the process rather than testing it, so tasklist is the only
    safe question to ask."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                                 capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return True                      # cannot tell - assume alive, not dead
        return str(pid) in out
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tail_events(log, keep=400):
    """The last timestamped runner lines, newest last. Worker output is written to
    its own attempt log, so run.log is already the summary line stream."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [m.groups() for m in (STAMP.match(l) for l in lines[-keep:]) if m]


def plan_state(spec):
    """(done, total, cost) from the plan, which IS the ledger."""
    try:
        text = spec.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0, 0.0
    return (len(DONE.findall(text)), len(STEP.findall(text)),
            sum(float(c) for c in COST.findall(text)))


def newest_run(root):
    runs = [d for d in (root / OUT).glob("*") if (d / "run.log").is_file()]
    return max(runs, key=lambda d: (d / "run.log").stat().st_mtime, default=None)


def since(stamp):
    try:
        delta = dt.datetime.now() - dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "?"
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes}m" if minutes < 90 else f"{minutes // 60}h{minutes % 60:02d}m"


def line(root):
    root = Path(root)
    name = root.name
    run = newest_run(root)
    if run is None:
        return f"{name}: no run directory yet"
    spec = root / "overnight" / "steps.yaml"
    done, total, cost = plan_state(spec)
    events = tail_events(run / "run.log")
    last = events[-1] if events else ("?", "(no log lines)")

    lock = root / OUT / ".lock"
    live = False
    if lock.is_file():
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
            live = pid_alive(held.get("pid"))
        except (ValueError, OSError):
            live = False

    if live:
        # PARKED is checked first. The last line naming a step is then HOURS old -
        # the step the run stopped on - and reporting it as what the run is doing
        # now is precisely the reading that makes a parked run look like a hung
        # one at 3am, which is the question this script exists to answer.
        latest = next((t for _, t in reversed(events)
                       if t.startswith(("PARKED after", "RESUMING:"))), "")
        if latest.startswith("PARKED"):
            waiting = next((t for _, t in reversed(events)
                            if t.lstrip().startswith("parked ")), "")
            return (f"{name} `{run.name}`: PARKED (waiting for the account, not stuck),"
                    f" {done}/{total} steps done, ${cost:.2f}"
                    f" | {waiting.strip()[:110]} | last line {since(last[0])} ago")
        # The most recent line that names a step is what it is doing now; the
        # heartbeat lines carry the elapsed minutes and the log size with them.
        doing = next((t for _, t in reversed(events) if t.startswith("[")), last[1])
        return (f"{name} `{run.name}`: RUNNING, {done}/{total} steps done, ${cost:.2f}"
                f" | {doing[:120]} | last line {since(last[0])} ago")
    # A finished run's own last line lists every outcome, which is a paragraph;
    # at 3am the useful half is WHY it stopped, so the roll-call is cut off.
    ended = next((t for _, t in reversed(events)
                  if t.startswith(("STOP:", "REFUSING", "run finished"))), last[1])
    ended = ended.split(" recorded): ")[0].split(". ")[0]
    return (f"{name} `{run.name}`: NOT RUNNING, {done}/{total} steps done, ${cost:.2f}"
            f" | {ended[:110]} | {since(last[0])} ago")


def main(argv):
    # The lines printed below are lifted from `run.log`, so they carry whatever a
    # WORKER wrote - a `<=`, an accent, a tick. Under a cp1252 console (Task
    # Scheduler's cmd.exe) the default `strict` errors make that a crash, which is
    # a poor answer to "how is it going". Lossy beats fatal; see forgiving_console
    # in overnight.py, where the same thing ended a live run on 2026-09-07.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    roots = argv[1:]
    if not roots:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    for root in roots:
        print(line(root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
