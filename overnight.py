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
"""overnight.py - run a steps specification as a sequence of headless Claude Code
workers, unattended, with gates, retries, review, reflection and a git undo.

The design in one paragraph. The orchestrator is a Python script and not an LLM,
so its context does not climb over a six-hour night. Every step is one
deliverable, small enough for one worker's context. A gate is a command that
exits 0 or non-zero, never a judgement: where an exit criterion is a claim about
behaviour, the brief names the test that encodes it and the gate runs that test.
Git is the undo: HEAD is recorded before every ATTEMPT and a failed gate resets to
it, so `bypassPermissions` is sandboxed by the repository. The runner does not own
the branch, though - a run is hours long and the operator, another window or a hook
can commit at any point inside a step - so every worker commits under a run-specific
committer identity, every commit a reset would discard is tagged and logged, and a
reset that would remove a FOREIGN commit is refused outright and the step halted.
Untracked files are moved into the run directory rather than cleaned away. Three
attempts; after
the second failure a diagnostic worker reads a COMPACT transcript of both and
writes a remediation plan the third ingests. A `review` step reads a passed
commit and returns a typed verdict; a `reflect` step may rewrite the PENDING part
of the spec, which the runner re-reads before every step and validates after
every rewrite. A step that still fails is STUCK and the run moves on.

The STEPS FILE IS THE LEDGER. There is no state.json: every outcome is spliced
into its own step's block as a `done:` mapping and committed, so what was
planned, what ran, when and what it produced are one hand-readable file under
version control. The splice is textual and verified against a re-parse - it never
re-dumps the file, because that would destroy the operator's comments.

    python -u overnight.py --spec overnight/steps.yaml
    python -u overnight.py --spec ... --list | --print-brief ID | --dry-run
    python -u overnight.py --spec ... --from ID | --only ID,ID
    python -u overnight.py --spec ... --fake-worker fake_worker.py   (self-test)

The steps spec format is `--format`, and is documented in references/spec-format.md
beside this file. By convention a project keeps its plan in `overnight/steps.yaml`
with briefs under `overnight/briefs/`; everything the run WRITES goes under
`overnight/runs/<run.name>/`, gitignored, so a worker's commits never carry the
run's logs and a reset never destroys a finding.

Without git - the binary absent, or the project not a repository - the run is
DEGRADED, not refused: there is no undo, so a failed gate cannot reset the tree,
clean_tree gates are skipped, review steps are skipped for want of a commit to
read, and a reflect step's rewrite is not committed. The condition is logged once
at the top of the run and carried into SUMMARY.md.

Invariants that are not negotiable, each learned the hard way:
  * never `--bare` (bills the API account, disables CLAUDE.md discovery);
  * strip ANTHROPIC_API_KEY, CLAUDE_EFFORT, CLAUDE_CODE_SUBAGENT_MODEL from the
    child environment;
  * briefs on STDIN, never argv (Windows truncates a command line at 32,767);
  * `git clean -fd`, never `-x` (ignored dirs hold the corpus and the logs);
  * `--tools` is variadic and swallows a following prompt - another reason for
    stdin;
  * a worker's output is `stream-json` so the log grows while it works, and a
    heartbeat logs the size every minute - a run that is silent until it ends
    cannot be told from a hang.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:                                   # pragma: no cover
    sys.exit("overnight.py needs pyyaml (pip install pyyaml)")

STRIP_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_EFFORT", "CLAUDE_CODE_SUBAGENT_MODEL")
# The per-project convention: everything an overnight run needs, and everything it
# writes, lives under `<project>/overnight/`. Only `runs/` and the decisions file
# are gitignored; the plan itself is committed, because the runner needs a clean
# tree and a reflect step commits its rewrite.
DEFAULT_OUT = "overnight/runs"
DEFAULT_DECISIONS = "overnight/DECISIONS-PENDING.md"

# Tools an unattended worker must not be able to reach, disallowed for every
# kind. Each of them acts OUTSIDE the run's tree, where the git undo does not
# reach: it publishes a page, schedules or triggers work that outlives the run,
# messages somebody, or fans out further agents. Nobody is awake to see any of
# it happen, and a reset cannot take it back. Blocking them also drops their
# definitions from the prefix every call re-reads, but that is worth about 4% of
# a run - the safety argument is the reason, the tokens are a bonus. A name the
# installed CLI does not have is inert, so the list can name a tool that only
# some versions ship.
UNATTENDED_DENY = ("Artifact", "CronCreate", "CronDelete", "CronList",
                   "DesignSync", "PushNotification", "RemoteTrigger",
                   "SendMessage", "Workflow")

NO_GIT_SHA = "(no-git)"                  # stands in for a commit id in a degraded run
NO_GIT_NOTICE = (
    "RUNNING WITHOUT A GIT UNDO: {reason}. A failed gate CANNOT reset the tree, so a"
    " worker's edits stay on disk; clean_tree gates are skipped; review steps are"
    " skipped (there is no commit to read); a reflect step's plan change is not"
    " committed. Gates and workers otherwise run normally.")

RERUN_OUTCOMES = {"STUCK", "FAIL", "INCONCLUSIVE", "SKIPPED", "REWORK FAILED",
                  "REVERTED BY REVIEW", "HALTED", "NOT RUN", "OVER BUDGET"}

# What a step gets when the WALL tripped during it: the workers produced nothing
# at all, so nothing about the code was learned and nothing about the code should
# be reported. Deliberately NOT `STUCK` - a STUCK step has had three real attempts
# and a diagnostic and is a finding about the work, whereas this one was never
# tested. It is in RERUN_OUTCOMES (a resume picks it straight back up) and out of
# BLOCKING_OUTCOMES (it does not need a person, it needs the quota back).
NOT_RUN = "NOT RUN"
WALL_NOTE = (
    "the worker could not run at all: {n} consecutive invocations returned nothing."
    " That is the environment, not this step - a usage limit, a logged-out CLI, a"
    " model withdrawn, the network gone. NOTHING WAS LEARNED ABOUT THIS STEP and"
    " nothing here is a finding about the code; it is left to be re-run.")

# What a build step gets when `run.budget_usd_per_step` tripped: the worker was
# cut off mid-work at a turn boundary having spent the entire cap. It does NOT
# retry. Three attempts against the same brief and the same cap spend the cap
# three times to be cut off at the same place three times - a $6 cap billing $18
# to learn nothing - because the cap is not what the worker got wrong. A step
# that cannot be done inside its budget is a PLANNING failure: the brief asks for
# too much, or the cap is set below what the work costs, and only a person can
# say which. So it is blocking as well as re-runnable - a resume picks it up
# again, but only after somebody has split the step or raised the cap.
OVER_BUDGET = "OVER BUDGET"
BUDGET_SUBTYPE = "error_max_budget_usd"
KINDS = ("build", "review", "reflect", "gate")
DEFAULT_TIERS = {
    "build": {"model": "opus", "effort": "medium"},        # OM
    "review": {"model": "opus", "effort": "high"},         # OH
    "reflect": {"model": "opus", "effort": "high"},        # OH
    "diagnostic": {"model": "opus", "effort": "high"},     # OH
}

# Every worker gets this, regardless of kind. `cat`/`sed` reads and greps done
# through Bash return whole files and cost far more input tokens than the
# equivalent Read/Grep/Glob call, which is why those exist as separate tools in
# the first place. The second half is the bigger lever of the two: measured over
# 14 real workers, re-reading the context was 56% of the bill, and a tool result
# is paid for once when it arrives and again on EVERY call after it - so a 50 KB
# file read at turn 3 of a 40-turn step is charged 38 more times. See
# docs/token-efficiency.md. Runner-level and not project-specific (rule: nothing
# project-specific in the runner), because it applies to every project the same
# way.
TOOL_USAGE_NOTE = (
    "Tool usage: prefer Read, Grep, Glob and an Explore-style subagent (if the"
    " Agent tool is available to you) for reading and searching the codebase over"
    " Bash. Reserve Bash for the test suite, build scripts and git. Do not use"
    " `cat` or `sed` to read a file - use Read.\n"
    "Keep tool results small: everything one returns stays in your context and is"
    " re-read on every call you make afterwards, which is the single largest cost"
    " of a step. So: locate first, read second - Grep for the symbol and read"
    " around the hits rather than reading a file to find them; on a large file"
    " Read the range you need with offset and limit, not the whole thing; do not"
    " re-read a file you have already read or just edited; and pipe a noisy"
    " command through a filter instead of dumping its output.\n"
    "Keep tool commands SHORT-RUNNING, and scope them to the question you are"
    " asking. Run the narrowest thing that can fail: a single test node before a"
    " test file, a test file before the suite. You never need to run the whole"
    " suite to show you are finished - this run's gates do that for you after you"
    " stop, and a green suite you ran yourself proves nothing the gate will not"
    " re-prove. The same goes for searching and building: a scoped path rather"
    " than the project root, an incremental build rather than a clean one. If a"
    " command has already taken more than about two minutes, do not run it again"
    " unchanged - narrow it. Waiting is not free: it is dead time against this"
    " step's clock, and a command that runs for several minutes can outlive the"
    " cache your context is served from, so the turn after it re-reads everything"
    " you have accumulated at full price instead of a tenth of it.\n")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "rework", "fail"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                "where": {"type": "string"},
                "what": {"type": "string"},
                "fix": {"type": "string"}},
            "required": ["severity", "where", "what", "fix"]}},
        "rework_brief": {"type": "string"},
    },
    "required": ["verdict", "summary", "findings", "rework_brief"],
}

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "changed": {"type": "boolean"},
        "rationale": {"type": "string"},
        "added": {"type": "array", "items": {"type": "string"}},
        "removed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["changed", "rationale", "added", "removed"],
}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
class Log:
    """The run's log. Opened by the FIRST line written, not by construction.

    A report-and-exit flag builds a Runner and logs nothing. Opening the file here
    left an empty `run.log` behind in a project that had never run a step - and
    `find_runs` looks for exactly that file, so `--progress` then reported a run
    IN FLIGHT that had never started. The existence of this file means a run
    started; nothing else may create it.
    """

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __call__(self, message, echo=True):
        """`echo=False` writes the line to the file and not to stdout.

        For the per-minute heartbeat only. A run is often launched from a Claude
        session that is orchestrating it, and every line on stdout costs that
        session a turn and re-bills its context - an expensive way to be told that
        nothing has changed. The file still gets every beat, which is what makes a
        hang distinguishable from progress.
        """
        line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a", encoding="utf-8", buffering=1)
        self.handle.write(line + "\n")
        self.handle.flush()
        if echo:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                # THE CONSOLE CANNOT REPRESENT SOMETHING A WORKER WROTE, and that
                # must never end a run. On 2026-09-07 it did: a worker's own
                # summary carried a `<=` sign, the 02:00 scheduled task ran under
                # cmd.exe at cp1252, and the runner died HERE - after the worker
                # had finished and committed, before the outcome was recorded. The
                # work survived, orphaned on a scratch branch, and the plan said
                # nothing had happened.
                #
                # Worker text is arbitrary and the console's encoding belongs to
                # whoever launched the run, so the only safe assumption is that
                # this line may be unprintable. The FILE already has it in full -
                # it is opened utf-8 above - so nothing is lost by degrading the
                # echo. main() also sets errors="replace" on the stream, which
                # normally makes this path unreachable; it is kept because a
                # stream that cannot be reconfigured is exactly the one that fails.
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def forgiving_console():
    """Never let an unprintable character end a run.

    A run's stdout belongs to whoever launched it: an interactive PowerShell is
    usually utf-8, Task Scheduler's cmd.exe is cp1252, and a redirect to a file
    takes the locale. The runner echoes worker-written text into that stream, and
    worker text is arbitrary - a `<=`, an accented name, a tick. Under cp1252 the
    default `strict` errors turn that into a UnicodeEncodeError that unwinds the
    whole run.

    So the ECHO is made lossy rather than fatal. The log file is opened utf-8
    independently and keeps every character, so this costs nothing but a `?` on a
    console that could not have shown the character anyway.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable TextIOWrapper - a StringIO under a test, a
            # closed stream. Log.__call__ carries the fallback for exactly this.
            pass


def child_env(extra=None):
    env = dict(os.environ)
    for key in STRIP_ENV:
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def pid_alive(pid):
    """Is this process id running? Never signals it.

    `os.kill(pid, 0)` is the POSIX idiom and is NOT portable here: on Windows
    os.kill ignores the signal and calls TerminateProcess, so the liveness check
    would kill the very run it is asking about.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                              capture_output=True, text=True, errors="replace")
        return str(pid) in (done.stdout or "")
    try:
        os.kill(pid, 0)                                  # signal 0: existence only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                      # someone else's, but alive
    return True


def kill_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True)
    else:
        process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def read_text(path, limit=None):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if limit and len(text) > limit:
        return text[:limit] + f"\n\n...[{len(text) - limit} chars elided]...\n"
    return text


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------
class SpecError(Exception):
    pass


def parse_until(text, now=None):
    """Resolve an `until` value to the absolute datetime the clock stops at.

    `--hours 7` asks the operator to do arithmetic at midnight against a number
    that is only ever a proxy for the thing they actually mean, which is a time
    of day: they know when they will be at the desk, not how many hours away
    that is. Worse, the arithmetic is done ONCE, at launch, so every minute
    spent typing the command comes off the end of the run.

    A bare `07:30` means the NEXT 07:30 - today's if it has not happened yet,
    tomorrow's if it has. That is what somebody typing it at 23:00 means, and it
    is the only reading under which the obvious overnight command works.

    A full `2026-09-08 07:30` is taken literally, and one already in the past is
    an ERROR rather than a silent roll forward to the next day: a dated stop
    time before the run starts is a typo, and rolling it forward would hide the
    typo behind a run that looked fine.
    """
    now = now or dt.datetime.now()
    raw = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            when = dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if when <= now:
            raise SpecError(
                f"until `{raw}` is already past ({now:%Y-%m-%d %H:%M}); the run"
                " would stop before starting a single step")
        return when
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            clock = dt.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
        when = now.replace(hour=clock.hour, minute=clock.minute,
                           second=clock.second, microsecond=0)
        if when <= now:
            when += dt.timedelta(days=1)
        return when
    raise SpecError(f"until `{raw}` is not a time: expected `HH:MM` for the next"
                    " occurrence of that time, or `YYYY-MM-DD HH:MM` for one"
                    " particular moment")


def load_spec(path):
    """Load and validate a steps spec. Raises SpecError with a plain reason."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or "steps" not in data:
        raise SpecError("spec must be a mapping with a `steps` list")
    run = data.get("run") or {}
    if not run.get("name"):
        raise SpecError("run.name is required (it names the output directory)")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise SpecError("`steps` must be a non-empty list")
    seen = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise SpecError(f"step {i} is not a mapping")
        sid = step.get("id")
        if not sid or not isinstance(sid, str):
            raise SpecError(f"step {i} has no id")
        if sid in seen:
            raise SpecError(f"duplicate step id {sid!r}")
        seen.add(sid)
        kind = step.get("kind", "build")
        if kind not in KINDS:
            raise SpecError(f"step {sid!r}: kind {kind!r} not in {KINDS}")
        step["kind"] = kind
        if kind == "build" and not step.get("brief"):
            raise SpecError(f"step {sid!r}: a build step needs a `brief` file")
        if kind == "review":
            if not step.get("of"):
                raise SpecError(f"step {sid!r}: a review step needs `of: <step id>`")
            if step["of"] not in seen:
                raise SpecError(f"step {sid!r}: reviews {step['of']!r}, which is not"
                                " an earlier step")
            step.setdefault("on_fail", "rework")
            if step["on_fail"] not in ("record", "rework", "revert"):
                raise SpecError(f"step {sid!r}: on_fail must be record|rework|revert")
        # Caught at load rather than at the worker: a mistyped cap would otherwise
        # surface as a crash in the middle of the night, several hours in.
        if "budget_usd" in step:
            try:
                if float(step["budget_usd"]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise SpecError(f"step {sid!r}: budget_usd must be a positive number,"
                                f" not {step['budget_usd']!r}")
        for gate in step.get("gates") or []:
            _validate_gate(gate, sid)
    for gate in run.get("gates") or []:
        _validate_gate(gate, "run")
    return data


def _validate_gate(gate, owner):
    if not isinstance(gate, dict):
        raise SpecError(f"{owner}: a gate must be a mapping")
    kinds = [k for k in ("cmd", "cmd_empty", "file", "fresh_shell", "clean_tree")
             if k in gate]
    if len(kinds) != 1:
        raise SpecError(f"{owner}: a gate has exactly one of cmd, cmd_empty, file,"
                        f" fresh_shell, clean_tree - got {kinds}")


def gate_name(gate):
    if "name" in gate:
        return gate["name"]
    for key in ("cmd", "cmd_empty", "file", "fresh_shell"):
        if key in gate:
            return f"{key}: {gate[key]}"
    return "tree clean (the worker committed)"


def dir_name(step_id):
    """A step id as a directory name.

    Step ids are addresses, not paths: the documented convention for a review is
    `review:<the step it reviews>`, and Windows forbids `:` in a filename. The run
    of 2026-09-05 crashed at its first review step, after four passes, because the
    id went to `mkdir` unchanged - and the self-test missed it by using `review-s1`
    where the convention and every real spec say `review:s1`.
    """
    return "".join("-" if ch in '<>:"/\\|?*' else ch for ch in step_id).rstrip(". ")


def completed_shape(step):
    """What a reflect step may NOT change on a step that has already run."""
    return json.dumps({k: step.get(k) for k in ("id", "kind", "brief", "of", "gates", "done")},
                      sort_keys=True)


def is_resumable(step):
    """True if this step still has work to do.

    A step is COMPLETE iff it carries `done:` with an outcome outside
    RERUN_OUTCOMES. A step that ran but did not complete carries `done:` and is
    still pending - which is why "has a done: block" is the wrong test and
    "resumable" is the right one.
    """
    done = step.get("done") or {}
    if not done:
        return True
    return done.get("outcome") in RERUN_OUTCOMES


# ---------------------------------------------------------------------------
# The ledger: outcomes are written back into the steps file itself
# ---------------------------------------------------------------------------
# The plan file is the single record of the run. There is no state.json: what was
# planned, what ran, when and what it produced all live in one hand-readable,
# version-controlled file, and a step's history sits next to the step.
#
# Which forces one rule above all others: SPLICE THE TEXT, NEVER RE-DUMP IT. The
# file is written by hand and carries comments, deliberate key order and quoting
# that `yaml.safe_dump` would silently destroy - and destroying the operator's
# comments as the price of recording an outcome is not a trade worth making.

def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _step_blocks(text):
    """[(step id, first line, last line + 1, key indent)] over the raw text.

    Located by scanning rather than by parsing, because the whole point is to put
    text back exactly where it came from. A step block starts at its `- id: <x>`
    line and runs to the next line at or left of the dash's indent.
    """
    lines = text.splitlines(keepends=True)
    blocks = []
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s*)-(\s+)id:\s*(.+?)\s*$", lines[index])
        if not match:
            index += 1
            continue
        dash_indent = len(match.group(1))
        key_indent = dash_indent + 1 + len(match.group(2))
        sid = match.group(3).strip().strip("'\"")
        end = index + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and _indent_of(line) <= dash_indent:
                break
            end += 1
        # Do not swallow blank lines or comments that belong to what comes next.
        while end > index + 1 and (not lines[end - 1].strip()
                                   or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        blocks.append((sid, index, end, key_indent))
        index = end
    return blocks


def _strip_done(lines, start, end, key_indent):
    """Remove an existing `done:` mapping from one step block. Returns new lines."""
    for i in range(start, end):
        if re.match(r"^\s*done:\s*$", lines[i]) and _indent_of(lines[i]) == key_indent:
            j = i + 1
            while j < end and (not lines[j].strip() or _indent_of(lines[j]) > key_indent):
                j += 1
            return lines[:i] + lines[j:], end - (j - i)
    return lines, end


def render_done(entry, key_indent):
    """The `done:` mapping as YAML text, at the step's key indent.

    Scalars go through json.dumps: a JSON string is a valid YAML double-quoted
    scalar, so a note containing a colon, a quote or a backslash cannot break the
    file - and `at:` MUST be quoted, because `2026-09-05 10:22` is not a string to
    a YAML parser.
    """
    pad = " " * key_indent
    out = [f"{pad}done:\n"]
    for key in ("outcome", "at", "sha", "attempts", "legs", "minutes", "cost_usd",
                "tier", "note", "reworked", "of", "findings", "added", "removed"):
        if key not in entry or entry[key] is None:
            continue
        value = entry[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = json.dumps(value)
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            rendered = "[" + ", ".join(json.dumps(str(v)) for v in value) + "]"
        else:
            rendered = json.dumps(str(value))
        out.append(f"{pad}  {key}: {rendered}\n")
    return "".join(out)


def upsert_done(text, step_id, entry):
    """Splice `done:` into one step's block, replacing any block already there.

    Raises SpecError if the result is not valid YAML or if any OTHER step's text
    changed - the verification that makes a textual edit safe to do unattended.
    """
    blocks = _step_blocks(text)
    target = next((b for b in blocks if b[0] == step_id), None)
    if target is None:
        raise SpecError(f"cannot record {step_id!r}: no `- id: {step_id}` in the spec text")
    _, start, end, key_indent = target
    lines = text.splitlines(keepends=True)
    lines, end = _strip_done(lines, start, end, key_indent)
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"                  # a spec whose last line lacks one
    new_text = "".join(lines[:end]) + render_done(entry, key_indent) + "".join(lines[end:])

    try:
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise SpecError(f"recording {step_id!r} produced invalid YAML: {exc}")
    if not isinstance(reparsed, dict) or "steps" not in reparsed:
        raise SpecError(f"recording {step_id!r} produced a spec with no steps")
    old_lines = text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    before = {sid: "".join(old_lines[a:b])
              for sid, a, b, _ in blocks if sid != step_id}
    after = {sid: "".join(new_lines[a:b])
             for sid, a, b, _ in _step_blocks(new_text) if sid != step_id}
    if before != after:
        changed = [k for k in before if before.get(k) != after.get(k)]
        raise SpecError(f"recording {step_id!r} would have changed other steps: {changed}")
    return new_text


def strip_all_done(text):
    """Remove every `done:` block. What --reset-state now does."""
    for _ in range(1000):
        removed = False
        for sid, start, end, key_indent in _step_blocks(text):
            lines = text.splitlines(keepends=True)
            new_lines, _ = _strip_done(lines, start, end, key_indent)
            if len(new_lines) != len(lines):
                text = "".join(new_lines)
                removed = True
                break
        if not removed:
            break
    return text


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, args):
        self.args = args
        self.spec_path = Path(args.spec).resolve()
        given_repo = Path(args.repo).resolve() if args.repo else self.spec_path.parent
        self.repo = given_repo
        while not (self.repo / ".git").exists() and self.repo.parent != self.repo:
            self.repo = self.repo.parent
        # No git is a DEGRADED run, not a refusal (see the module docstring). The
        # undo disappears; the gates and the workers still work.
        self.git_exe = shutil.which("git")
        if not (self.repo / ".git").exists():
            # No repository to root the project at, so fall back to the launching
            # shell's directory when the spec is inside it - which it is, for the
            # documented `cd <project>; overnight.py --spec overnight/steps.yaml`.
            # The spec's own parent is `<project>/overnight/` and would put every
            # relative gate and brief path one level too deep.
            cwd = Path.cwd().resolve()
            self.repo = cwd if str(self.spec_path).startswith(str(cwd)) else given_repo
            self.no_git_reason = ("no git repository at or above the spec"
                                  f" ({given_repo})")
        elif not self.git_exe:
            self.no_git_reason = "git is not on PATH"
        else:
            self.no_git_reason = ""
        self.has_git = not self.no_git_reason
        self.spec = load_spec(self.spec_path)
        self.run_cfg = self.spec.get("run") or {}
        self.out = self.repo / self.run_cfg.get("out", DEFAULT_OUT) \
            / self.run_cfg["name"]
        if args.dry_run:
            # A rehearsal writes nothing a real run would read. Its log, its stub
            # attempts and its summary go one level down, so a dry run on the
            # morning after cannot overwrite last night's attempt-1.log or
            # SUMMARY.md - and find_runs, which globs `runs/*/run.log`, does not
            # mistake the rehearsal for a run.
            self.out = self.out / "dry-run"
        # The directory is NOT created here. Building a Runner is what --list and
        # --print-brief do, and a report must not leave a run directory behind in a
        # project that has never run one. Runner.main() makes it.
        self.log = Log(self.out / "run.log")
        # Set BEFORE anything can commit. Every commit this run makes - a worker's
        # work, a ledger entry, a reflect's plan change - carries this committer
        # address, which is how safe_reset tells the run's own commits from a third
        # party's. A ledger commit that looked foreign would make the runner refuse
        # to reset over its own bookkeeping.
        self.committer_email = f"overnight+{self.ref_safe(self.run_cfg['name'])}@runner.invalid"
        self.committer_name = f"overnight worker ({self.run_cfg['name']})"
        self.step_cost = {}
        # THIS SESSION's spend, as against cost_so_far(), which sums the ledger and
        # so carries every step the plan has ever run. Both are wanted at the end -
        # one says what tonight cost, the other what the plan has cost - and the
        # final line used to print the second under wording that read like the first.
        self.session_cost = 0.0
        # THE TREE THE CURRENT STEP WORKS IN. It is the repository itself for
        # everything except an isolated build step, which sets it to a private
        # worktree for the length of the step. Gates, the worker's cwd and every
        # git call about the work in progress go here; the ledger and the plan file
        # are always the repository, because that is where they live.
        self.tree = self.repo
        # THE DEFAULT IS `worktree`, and that is a correctness decision, not a
        # tidiness one. In-place, a build step's clean_tree gate reads the tree the
        # operator is also using, and cannot tell the worker's uncommitted work
        # from a file somebody else created mid-step. On 2026-09-06 that failed a
        # good build for the operator's own session log, discarded it, and relaunched
        # into the same unwinnable state - a repeating, retrying, billable loss of
        # work, with the worker penalised for obeying its brief. Isolated, the
        # operator's file lands in the operator's tree and the gate never sees it.
        # `in-place` remains for a project whose gates need state git does not
        # carry and that cannot be linked; preflight_worktree says so out loud
        # rather than letting it be discovered at 03:00.
        self.isolation = str(self.run_cfg.get("isolation", "worktree")).strip()
        if self.isolation not in ("in-place", "worktree"):
            raise SpecError(f"run.isolation must be `in-place` or `worktree`,"
                            f" not `{self.isolation}`")
        if self.isolation == "worktree" and not self.has_git:
            self.isolation = "in-place"
            self._isolation_note = ("isolation `worktree` needs git; falling back to"
                                    f" in-place ({self.no_git_reason})")
        # Constructing a Runner does nothing. --reset-state used to strip the
        # ledger HERE, as a side effect, and main() then fell through into a real
        # launch: the operator asked to forget the outcomes and got live workers.
        # It is an exclusive action now, dispatched by main() beside --list.
        self.claude = args.fake_worker or shutil.which("claude")
        self.started = dt.datetime.now()
        # THE CLOCK, and which of four sources set it. The banner names the
        # source and the resolved absolute time, because the one thing an
        # operator cannot check at 03:00 is an assumption they made at 23:00.
        self.stop_at, self.clock_source = self.resolve_clock()
        self.hours = (self.stop_at - time.time()) / 3600
        self._ran_this_session = set()
        # -- the wall -------------------------------------------------------
        # A worker that returns NOTHING is not a worker that wrote bad code, and
        # the runner used to be unable to tell the two apart: on 2026-09-07 the
        # account's five-hour window closed mid-run and every remaining step was
        # retried, diagnosed, marked STUCK and abandoned in seconds each, so the
        # morning showed a plan whose every step needed re-running and a summary
        # full of findings about code no worker had ever looked at.
        #
        # The signal is deliberately NOT the error text. Parsing for a quota
        # message would be brittle and would miss the other ways an environment
        # goes away, so what is counted is barren invocations: the worker failed
        # AND produced no result event at all. N of those in a row means nothing
        # can succeed right now, whatever the cause.
        self.on_wall = str(self.args.on_wall or self.run_cfg.get("on_wall", "park")).strip()
        if self.on_wall not in ("park", "stop"):
            raise SpecError(f"run.on_wall must be `park` or `stop`, not `{self.on_wall}`")
        self.wall_threshold = int(self.args.wall_threshold
                                  or self.run_cfg.get("wall_threshold", 3))
        self.park_poll_min = float(self.args.park_poll_min
                                   or self.run_cfg.get("park_poll_min", 30))
        # A worker that has written NOTHING for this long is wedged, not busy.
        # Both slow paths keep writing: a Bash call over ~30s emits a
        # `tool_progress` heartbeat every 30s, and a long generation emits
        # `thinking_tokens` records throughout - so silence means neither is
        # happening. The default is measured, not guessed: over 25 real worker
        # logs the largest silence a WORKING worker ever produced was 291s
        # (4.9 min) and the median per-log maximum was 81s, so 10 minutes is
        # about twice the worst case ever seen. `worker_timeout_min` cannot do
        # this job - it has to be set for the slowest legitimate step, so it can
        # never catch a stall early. 0 turns the watchdog off.
        self.stall_min = float(self.args.stall_min
                               if self.args.stall_min is not None
                               else self.run_cfg.get("stall_min", 10))
        # How many EXTRA workers one attempt may use when the budget cuts one off
        # part-way. The cap is per invocation, so a continuation gets a fresh one:
        # this is the difference between spending it again on PROGRESS and spending
        # it again on REPETITION, which is what a retry does. Default 0 - off -
        # because an existing plan's cap is a backstop sized well above a good step
        # (the README recommends 45), and quietly allowing a second worker would
        # let a trip at $45 spend $90 on a plan nobody re-tuned. Continuation is
        # what makes a SMALL per-step cap usable, so it is turned on beside one.
        self.continuations = int(self.args.continuations
                                 if self.args.continuations is not None
                                 else self.run_cfg.get("continuations", 0))
        self.barren = 0            # consecutive worker invocations that produced nothing
        self.parked_seconds = 0.0  # for the summary: how much of the night went to waiting
        self.probes = 0
        self.probe_cost = 0.0

    # -- the ledger ----------------------------------------------------------
    def resolve_clock(self):
        """Return (stop_at epoch, a phrase naming what set it).

        Four sources, in this order:

            --until (CLI)  >  --hours (CLI)  >  run.until (spec)  >  run.hours

        CLI beats spec FIRST, and only then does `until` beat `hours` within a
        level. The other reading - `until` winning wherever it appears - makes a
        spec carrying `until` silently swallow a typed `--hours`, and a flag that
        is quietly inert is worse than one that is refused: the operator watches
        the run stop at a time they explicitly overrode and has nothing to blame.
        """
        now = dt.datetime.now()
        if self.args.until:
            return parse_until(self.args.until, now).timestamp(), \
                f"--until {self.args.until}"
        if self.args.hours is not None:
            return time.time() + self.args.hours * 3600, f"--hours {self.args.hours:g}"
        if self.run_cfg.get("until"):
            raw = str(self.run_cfg["until"])
            return parse_until(raw, now).timestamp(), f"run.until `{raw}` in the spec"
        if self.run_cfg.get("hours") is not None:
            hours = float(self.run_cfg["hours"])
            return time.time() + hours * 3600, f"run.hours {hours:g} in the spec"
        return time.time() + 6 * 3600, "the default of 6 h (no until, no hours)"

    def done_map(self, spec=None):
        """{step id: its done: mapping} for every step that has run."""
        steps = (spec or self.spec)["steps"]
        return {s["id"]: s["done"] for s in steps if s.get("done")}

    def cost_so_far(self):
        """The run's cost is the sum of the ledger. Nothing else accumulates it."""
        return round(sum(float(d.get("cost_usd") or 0) for d in self.done_map().values()), 2)

    def record(self, step, outcome, **fields):
        """Write the outcome into the step's own block in the spec, and commit it.

        Re-reads the file immediately before splicing rather than using a copy
        taken at step start, because the operator may hand-edit a PENDING step
        while the run is live and that edit must survive this write. The write is
        atomic (temp file, os.replace) so a crash mid-record cannot leave a
        half-written plan.
        """
        entry = {"outcome": outcome,
                 "at": f"{dt.datetime.now():%Y-%m-%d %H:%M}"}
        # `expected_min` is a plan-time estimate and stays where the operator put
        # it, in the step itself; the ACTUAL goes here beside it. An overrun is the
        # symptom of a step carrying more than one deliverable, and the next plan
        # can only be calibrated against numbers somebody kept.
        for key in ("sha", "attempts", "minutes", "cost_usd", "tier", "note",
                    "legs", "reworked", "of", "findings", "added", "removed"):
            if key in fields and fields[key] not in (None, "", []):
                entry[key] = fields[key]
        if entry.get("sha") in (None, NO_GIT_SHA):
            entry.pop("sha", None)
        elif isinstance(entry.get("sha"), str):
            entry["sha"] = entry["sha"][:8]
        if "cost_usd" not in entry:
            spent = self.step_cost.pop(step["id"], 0.0)
            if spent:
                entry["cost_usd"] = round(spent, 2)
        self.session_cost += float(entry.get("cost_usd") or 0)
        self.write_ledger(step["id"], entry)
        # The long worker summary does NOT go in the yaml - it would make the plan
        # unscannable. It goes beside the logs, where review_brief reads it from.
        if fields.get("summary"):
            step_dir = self.out / dir_name(step["id"])
            step_dir.mkdir(parents=True, exist_ok=True)
            (step_dir / "summary.md").write_text(fields["summary"], encoding="utf-8")

    def write_ledger(self, step_id, entry, why=""):
        if self.args.dry_run:
            # A dry run does not get a vote on the plan. This is the single choke
            # point for every splice - record, update_done, a rework, a revert - so
            # the whole ledger is read-only for the duration. It once wrote a real
            # committed STUCK here, indistinguishable from an exhausted real
            # attempt, after three stub attempts failed a gate no worker had been
            # asked to satisfy; the plan then read BLOCKED and needed --reset-state
            # to undo. The rehearsal reports what it would have written instead.
            said = ", ".join(f"{k}={v}" for k, v in entry.items())
            self.log(f"    (dry run) the plan is NOT written. Would have recorded"
                     f" {step_id}: {said}")
            return False
        text = self.spec_path.read_text(encoding="utf-8")
        try:
            new_text = upsert_done(text, step_id, entry)
        except SpecError as exc:
            self.log(f"    COULD NOT RECORD {step_id} in the plan: {exc}")
            self.log(f"    the outcome was {entry.get('outcome')}; resume will re-run it")
            return False
        self.atomic_write(new_text)
        self.spec = load_spec(self.spec_path)
        self.commit_ledger(step_id, why or entry.get("outcome", ""))
        return True

    def atomic_write(self, text):
        temp = self.spec_path.with_suffix(self.spec_path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.spec_path)

    def commit_ledger(self, step_id, outcome):
        """Commit the plan file alone, as the run's own identity.

        The identity matters: safe_reset partitions `base..HEAD` by committer, and
        a ledger commit that looked foreign would make the runner refuse to reset
        over its own bookkeeping.
        """
        if not self.has_git:
            return
        # ALWAYS the repository, never the step's worktree: the plan file is the
        # ledger and it lives here. A worktree is on a scratch branch, so a ledger
        # commit made there would land where nobody reads it.
        rel = self.spec_path.relative_to(self.repo).as_posix()
        self.git("add", rel, tree=self.repo)
        code, output = self.git("commit", "-q", "-m",
                                f"overnight: {step_id} {outcome}", "--", rel,
                                tree=self.repo)
        if code != 0 and "nothing to commit" not in output:
            self.log(f"    could not commit the ledger for {step_id}: {output[:200]}")

    def update_done(self, step_id, changes, why=""):
        """Merge into a step's EXISTING done: block, replacing it in place.

        A rework or a revert-by-review changes the outcome of a step that has
        already been recorded. There must be one `done:` per step, never two -
        and the commit says WHY it was rewritten, because a rework leaves the
        outcome at PASS and two identical `overnight: <id> PASS` lines in the log
        would tell the morning nothing.
        """
        entry = dict(self.done_map().get(step_id) or {})
        entry.update(changes)
        entry["at"] = f"{dt.datetime.now():%Y-%m-%d %H:%M}"
        if isinstance(entry.get("sha"), str):
            entry["sha"] = entry["sha"][:8]
        self.write_ledger(step_id, entry, why=why)

    def reset_ledger(self):
        """--reset-state: strip every done: block. The flag outlives the file.

        Reports to stdout, not to run.log: this is not part of a run, and writing
        the log would create the very file `find_runs` reads as "a run started
        here". The durable record of a reset is its commit.
        """
        text = self.spec_path.read_text(encoding="utf-8")
        stripped = strip_all_done(text)
        if stripped == text:
            print("--reset-state: the plan carries no outcomes; nothing to forget")
            return
        self.atomic_write(stripped)
        self.spec = load_spec(self.spec_path)
        print("--reset-state: every `done:` stripped from the plan")
        self.commit_ledger("--reset-state", "forget every outcome")

    # -- the lock ------------------------------------------------------------
    def lock_path(self):
        # Beside the run directories, which are gitignored, and NOT inside any one
        # run's directory: the lock is the repository's, not the run's.
        return self.repo / self.run_cfg.get("out", DEFAULT_OUT) / ".lock"

    def acquire_lock(self):
        """One runner per repository at a time.

        Per REPOSITORY, not per run: two runs with different names still share one
        branch, one working tree and one plan file, so the first runner's reset can
        still destroy the second's work. Refusing to start is the cheap half of
        roadmap item 2; worktrees are the half that removes the conflict itself.
        """
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                held = {}
            pid, host = int(held.get("pid") or 0), held.get("host", "?")
            # A pid is only meaningful on the machine that wrote it. On any other
            # host - a network share, a container mount - liveness cannot be
            # checked, so the conservative answer is the only honest one.
            elsewhere = host != socket.gethostname()
            if held and (elsewhere or pid_alive(pid)):
                self.log(f"REFUSING TO START: a run is already holding this repository."
                         f" `{held.get('run', '?')}` (pid {pid} on {host}, started"
                         f" {held.get('started', '?')}, spec {held.get('spec', '?')}).")
                if elsewhere:
                    self.log(f"    that lock was taken on another machine ({host}), so"
                             " this one cannot tell whether it is still alive.")
                self.log(f"    if that run is over, delete {path} and start again.")
                raise SystemExit(2)
            self.log(f"    taking over a stale lock from pid {pid} ({host}), which is"
                     " no longer running")
        path.write_text(json.dumps(
            {"pid": os.getpid(), "host": socket.gethostname(), "run": self.run_cfg["name"],
             "spec": str(self.spec_path), "started": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"},
            indent=2), encoding="utf-8")
        self._holds_lock = True

    def release_lock(self):
        """Only ever removes a lock this process actually took."""
        if getattr(self, "_holds_lock", False):
            try:
                self.lock_path().unlink()
            except OSError:
                pass
            self._holds_lock = False

    # -- worktree isolation --------------------------------------------------
    def worktree_root(self):
        """OUTSIDE the repository, deliberately.

        A universal gate like `pytest -q` walks the project from its root and does
        not read .gitignore: a worktree under `overnight/runs/` would have every
        test file collected twice under one module name, and the universal gate
        would fail for a reason nothing to do with the worker. That is the same
        defect the `.quarantined` suffix exists to avoid, at a hundred times the
        scale. A sibling of the repository is easy to find and collected by
        nothing.
        """
        configured = self.run_cfg.get("worktree_root")
        root = Path(configured) if configured else \
            self.repo.parent / f"{self.repo.name}.overnight-worktrees"
        return root / self.ref_safe(self.run_cfg["name"])

    def scratch_branch(self, step_id):
        return f"overnight/{self.ref_safe(self.run_cfg['name'])}/{self.ref_safe(step_id)}"

    def link_into_worktree(self, path):
        """Link what git does not carry: .venv, node_modules, a local .env.

        The reason a worktree is dangerous at all. A fresh checkout has none of
        these, so gates that pass in the operator's tree fail in the worktree for
        reasons that have nothing to do with the worker. Linked, never copied: a
        copied virtualenv is stale the moment anything installs into the real one.
        """
        for rel in self.run_cfg.get("worktree_link") or []:
            source, target = self.repo / rel, path / rel
            if not source.exists() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if os.name == "nt" and source.is_dir():
                    # A junction needs no administrator rights; a symlink does.
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                                   capture_output=True, check=True)
                else:
                    target.symlink_to(source, target_is_directory=source.is_dir())
                self.log(f"    linked {rel} into the worktree")
            except (OSError, subprocess.CalledProcessError) as exc:
                self.log(f"    COULD NOT link {rel} into the worktree: {exc}."
                         " A gate that needs it will fail there.")

    def rescue_scratch_branch(self, step_id, base):
        """`worktree add -B` force-moves the branch. Tag whatever that orphans.

        A crashed run leaves its work committed on the step's scratch branch and
        NOWHERE ELSE - the worktree that held it is gone and the branch was never
        integrated. Re-running that step resets the branch to `base`, and those
        commits become unreachable with no tag, no note, and no way for the
        morning to learn they ever existed. `safe_reset` has tagged before
        discarding since the beginning; this is that same rule applied at the
        other place the runner moves a ref.
        """
        branch = self.scratch_branch(step_id)
        if self.git("rev-parse", "--verify", "--quiet", branch, tree=self.repo)[0] != 0:
            return
        losing = self.commits_since(base, tip=branch, tree=self.repo)
        if not losing:
            return
        step_dir = self.out / dir_name(step_id)
        step_dir.mkdir(parents=True, exist_ok=True)
        record = [f"# commits an earlier run left on `{branch}`", "",
                  f"The scratch branch is being reset to `{base[:8]}` so this step"
                  " can run again. These commits were on it and are not on the"
                  " operator's branch:", ""]
        for index, (sha, email, subject) in enumerate(losing, 1):
            tag = f"rescue/{self.ref_safe(step_id)}/scratch-{index}"
            self.git("tag", "-f", tag, sha, tree=self.repo)
            self.log(f"    RESCUING {sha[:8]} left on {branch} by an earlier run"
                     f" - kept as tag `{tag}`: {subject[:80]}")
            record.append(f"- `{sha}` <{email}> tag `{tag}`: {subject}")
        with (step_dir / "discarded-commits.md").open("a", encoding="utf-8") as handle:
            handle.write("\n".join(record) +
                         "\n\nRecover one with `git cherry-pick <sha>`;"
                         " drop the tags with `git tag -d <tag>` when done.\n\n")

    def retest_stranded(self, step, step_dir, base):
        """Work a crashed run left on the scratch branch: replay it, gate it, and
        keep it if it passes.

        THE FIELD CASE, 2026-09-07. A worker finished at 02:32:39 - exit 0, gates
        passed, tree clean, work committed - and the run died before integrating
        it. The relaunch could not create the worktree and recorded `STUCK,
        attempts: 0, could not create the worktree for this step`, which reads as
        *nothing happened*. Eleven minutes and five dollars of finished work sat
        on the scratch branch, and the natural next move - re-run the step -
        rebuilds every bit of it.

        Reporting that to the operator was the weaker answer. **The gate is
        already the arbiter of whether work is good**, everywhere else in this
        runner: work that passes merges exactly as a successful re-run's work
        would, and work that fails is discarded exactly as a failed attempt's is.
        Neither needs a person, and neither needs a new outcome.

        The replay is not optional. The stranded commit was built against an
        OLDER base; gating it where it was built would prove only that it used to
        work, which is not the question. It is replayed onto the current base
        first, and that is what makes the test honest.

        This must run BEFORE `worktree add -B`, which force-moves the branch to
        base and puts these commits out of reach.

        Returns a result to record, or None to run the step normally.
        """
        if not self.has_git or self.args.dry_run:
            return None
        branch = self.scratch_branch(step["id"])
        if self.git("rev-parse", "--verify", "--quiet", branch, tree=self.repo)[0] != 0:
            return None
        stranded = self.commits_since(base, tip=branch, tree=self.repo)
        if not stranded:
            return None
        self.log(f"[{step['id']}] an earlier run left {len(stranded)} commit(s) on"
                 f" `{branch}`. Replaying and gating them BEFORE spending a worker.")
        for sha, _email, subject in stranded:
            self.log(f"    {sha[:8]} {subject[:80]}")

        path = self.worktree_root() / f"{self.ref_safe(step['id'])}-stranded"
        if path.exists():
            self.remove_worktree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add", str(path), branch]
        code, output = self.git(*args, tree=self.repo)
        if code != 0 and "already used by worktree" in output:
            self.git("worktree", "prune", tree=self.repo)
            code, output = self.git(*args, tree=self.repo)
        if code != 0:
            # Fall through rather than fail: the normal path tags this work before
            # it resets the branch, so nothing is lost by not having tested it.
            self.log(f"    could not check the stranded work out: {output[:200]}")
            return None
        try:
            self.link_into_worktree(path)
            code, fork = self.git("merge-base", branch, base, tree=self.repo)
            if code != 0:
                self.log(f"    no merge base with the branch: {fork[:200]}")
                return None
            # Only when the branch has actually moved under it. `git rebase` over a
            # no-op is free to rewrite the commits it replays, and the sha this
            # work is tagged under if it fails the gate has to be the one the
            # morning was told about.
            code = 0
            if fork != base:
                code, output = self.git("rebase", "--onto", base, fork, tree=path)
            if code != 0:
                self.git("rebase", "--abort", tree=path)
                note = (f"an earlier run left {len(stranded)} commit(s) on `{branch}`"
                        " and they do not replay onto the branch, so they could not"
                        " be tested. Merge them by hand (`git rebase`/`git"
                        " cherry-pick`), or drop the branch and re-run this step.")
                self.log(f"[{step['id']}] NEEDS MERGE - {note}")
                return {"outcome": "NEEDS MERGE", "attempts": 0, "note": note}
            step_dir.mkdir(parents=True, exist_ok=True)
            with (step_dir / "stranded.log").open("w", encoding="utf-8") as handle:
                handle.write(f"Work an earlier run left on {branch}, replayed onto"
                             f" {base[:8]} and put through this step's gates.\n")
                for sha, email, subject in stranded:
                    handle.write(f"  {sha} <{email}> {subject}\n")
                handle.write("\n=== GATES ===\n")
                outer, self.tree = self.tree, path
                try:
                    ok, label, _out = self.run_gates(self.all_gates(step), handle)
                finally:
                    self.tree = outer
        finally:
            if path.exists():
                self.remove_worktree(path)
        if not ok:
            # Exactly what a failed attempt gets: tagged and listed by
            # rescue_scratch_branch on the way into the normal run below.
            self.log(f"[{step['id']}] the stranded work FAILS this step's gates"
                     f" ({label}). Discarding it and running the step for real.")
            return None
        status, sha = self.integrate(step["id"], base, branch)
        if status == "conflict":
            note = (f"the work an earlier run left on `{branch}` passed this step's"
                    " gates but would not land on the branch. Merge it by hand.")
            self.log(f"[{step['id']}] NEEDS MERGE - {note}")
            return {"outcome": "NEEDS MERGE", "attempts": 0, "note": note}
        note = (f"an earlier run built this step and died before integrating it."
                f" Its work replayed onto the branch, PASSED this step's gates"
                f" unchanged, and was {status} at {sha[:8]}."
                " NO WORKER WAS SPENT on it in this run.")
        self.log(f"[{step['id']}] PASS from an earlier run's work - {note}")
        return {"outcome": "PASS", "attempts": 0, "sha": sha, "note": note}

    def add_worktree(self, step_id, base, detach=False):
        """A private tree on a scratch branch, based at `base`."""
        path = self.worktree_root() / (self.ref_safe(step_id) if not detach else "_probe")
        if path.exists():
            self.remove_worktree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not detach:
            self.rescue_scratch_branch(step_id, base)
        args = ["worktree", "add", "--detach", str(path), base] if detach else \
            ["worktree", "add", "-B", self.scratch_branch(step_id), str(path), base]
        code, output = self.git(*args, tree=self.repo)
        if code != 0 and "already used by worktree" in output:
            # A registration git still holds for a directory that is no longer
            # there. Prune it and try once more: a leftover from a crashed run
            # must not cost a step its night, which is exactly what it did on
            # 2026-09-07 - STUCK at zero attempts, no code read, nothing spent.
            self.log("    a stale worktree registration is in the way; pruning and retrying")
            self.git("worktree", "prune", tree=self.repo)
            code, output = self.git(*args, tree=self.repo)
        if code != 0:
            self.log(f"    COULD NOT create a worktree at {path}: {output[:300]}")
            return None
        self.link_into_worktree(path)
        return path

    def remove_worktree(self, path, keep_branch=True):
        """Removes the DIRECTORY. Never the branch unless it is merged.

        An unmerged scratch branch is somebody's work - a step whose integration
        conflicted keeps its commits there and the morning report names it.
        Deleting it silently is the exact class this whole design closes.
        """
        self.git("worktree", "remove", "--force", str(path), tree=self.repo)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if not keep_branch:
            self.git("branch", "-d", str(path.name), tree=self.repo)

    @staticmethod
    def _path_key(path):
        """A path in one canonical form, for comparing two of them."""
        try:
            path = Path(path).resolve()
        except OSError:
            path = Path(path)
        return os.path.normcase(str(path))

    def registered_worktrees(self):
        """The paths git currently holds a registration for. None if it cannot say.

        NEVER compare this output as text. `worktree list --porcelain` prints
        FORWARD slashes on every platform; `str(path)` on Windows prints
        BACKslashes, so `str(path) not in known` was true for every registered
        worktree there and prune_worktrees deleted all of them. Resolved paths,
        case-normalised, or nothing.
        """
        code, output = self.git("worktree", "list", "--porcelain", tree=self.repo)
        if code != 0:
            return None                 # "I do not know" is not "there are none"
        return {self._path_key(line[len("worktree "):].strip())
                for line in output.splitlines() if line.startswith("worktree ")}

    def prune_worktrees(self):
        """After a crash: git forgets the registration, the directory remains.

        Only a directory git does NOT know about is an orphan. One it does know
        about is a live worktree, and tearing that off the filesystem is worse
        than leaving it: the registration is then dangling, the next
        `worktree add -B` for its branch fails `already used by worktree`, and
        the step goes STUCK at zero attempts. That is the 2026-09-07 field
        defect, and it cost a run its night.
        """
        self.git("worktree", "prune", tree=self.repo)
        root = self.worktree_root()
        if not root.exists():
            return
        known = self.registered_worktrees()
        if known is None:
            self.log("    could not list git's worktrees; leaving any leftovers alone")
            return
        removed = False
        for path in sorted(root.iterdir()):
            if path.is_dir() and self._path_key(path) not in known:
                self.log(f"    removing an orphaned worktree from an earlier run: {path}")
                shutil.rmtree(path, ignore_errors=True)
                removed = True
        if removed:
            # Anything the removal has just made stale goes now, rather than
            # surviving into the first `worktree add` of the run.
            self.git("worktree", "prune", tree=self.repo)

    def integrate(self, step_id, base, branch):
        """Bring the worktree's commits onto the operator's branch.

        Returns (status, sha) with status "nothing", "ff", "replayed" or "conflict".
        The sha is the one ON THE OPERATOR'S BRANCH - a replay rewrites it, and the
        ledger's sha is what the reviewer reads and the morning inspects.
        """
        made = self.commits_since(base, tip=branch, tree=self.repo)
        if not made:
            return "nothing", self.head_of(self.repo)
        head = self.head_of(self.repo)
        if head == base:
            code, output = self.git("merge", "--ff-only", branch, tree=self.repo)
            if code == 0:
                return "ff", self.head_of(self.repo)
            self.log(f"    fast-forward refused unexpectedly: {output[:200]}")
        # The branch moved under the step - the operator committed, or an earlier
        # step did. Replaying is the whole point: today this is where the step is
        # HALTED and its work discarded.
        code, output = self.git("rebase", "--onto", head, base, branch, tree=self.repo)
        if code != 0:
            self.git("rebase", "--abort", tree=self.repo)
            self.log(f"    the work on `{branch}` does not replay onto the branch:"
                     f" {output[-400:]}")
            return "conflict", ""
        code, output = self.git("merge", "--ff-only", branch, tree=self.repo)
        if code != 0:
            self.log(f"    replayed but could not fast-forward: {output[:200]}")
            return "conflict", ""
        return "replayed", self.head_of(self.repo)

    def head_of(self, tree):
        if not self.has_git:
            return NO_GIT_SHA
        return self.git("rev-parse", "HEAD", tree=tree)[1]

    # -- git -----------------------------------------------------------------
    def git(self, *args, timeout=300, tree=None):
        # The run's own identity on every git call, so the runner's commits - the
        # ledger and the reflect's plan change - are classified as this run's by
        # safe_reset exactly as a worker's are. GIT_AUTHOR_* is deliberately left
        # alone: authorship stays whatever the project's git config says, so the
        # history reads normally.
        env = child_env({"GIT_COMMITTER_NAME": self.committer_name,
                         "GIT_COMMITTER_EMAIL": self.committer_email})
        done = subprocess.run(["git", *args], cwd=(tree or self.tree), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return done.returncode, (done.stdout + done.stderr).strip()

    def head(self):
        if not self.has_git:
            return NO_GIT_SHA
        return self.git("rev-parse", "HEAD")[1]

    def work_since(self, base):
        """What the tree holds that the attempt's baseline did not, as short text.

        This IS the handover: the commits a cut-off worker managed to make, and the
        files it still had open when the money ran out. Deliberately bounded - it
        goes into the next worker's brief, and every character of a brief is
        re-read on every one of that worker's turns, so a full diff here would be
        charged dozens of times over.
        """
        if not self.has_git:
            return ""
        bits = []
        code, commits = self.git("log", "--oneline", f"{base}..HEAD")
        if code == 0 and commits.strip():
            bits.append("Commits it managed to make:\n" + commits.strip()[:1500])
        code, dirty = self.git("status", "--porcelain")
        if code == 0 and dirty.strip():
            bits.append("Left uncommitted in the tree:\n" + dirty.strip()[:1500])
        return "\n\n".join(bits)

    def tree_state(self):
        """A cheap fingerprint of the work tree: HEAD, and everything uncommitted.

        It answers one question, asked either side of a worker: did that worker
        CHANGE anything? A worker that spent an entire budget and left the tree
        byte-identical has produced nothing to hand on, and that is the runaway
        signature - confidently going nowhere - as opposed to a big step that ran
        out of money with real work on disk.

        Without git there is no cheap fingerprint and no reset either, so it
        returns None and the caller treats progress as unknowable.
        """
        if not self.has_git:
            return None
        code, out = self.git("status", "--porcelain")
        return (self.head(), out) if code == 0 else None

    def commits_since(self, base, tip="HEAD", tree=None):
        """[(sha, committer email, subject)] reachable from `tip` but not from `base`.

        `tip` is not always HEAD: after an isolated step the commits to integrate
        are on a scratch branch, and the worktree that held them has already been
        removed so the branch can be checked out for the replay.
        """
        if not self.has_git:
            return []
        code, output = self.git("log", "--format=%H%x1f%ce%x1f%s", f"{base}..{tip}",
                                tree=tree)
        if code != 0:
            return []
        rows = []
        for line in output.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                rows.append(tuple(parts))
        return rows

    def ref_safe(self, label):
        return "".join("-" if ch in '~^:?*[\\ ' else ch for ch in label).strip("-.")

    def quarantine_untracked(self, step_dir, label):
        """Move what `git clean -fd` would delete instead of deleting it.

        The run is hours long and the repository is not frozen: an untracked file
        may be a scratch note somebody else was in the middle of. Deleting it is
        both silent and unrecoverable, and moving it costs nothing.
        """
        code, output = self.git("clean", "-nd")
        paths = [line[len("Would remove "):].strip()
                 for line in output.splitlines() if line.startswith("Would remove ")]
        if not paths:
            return []
        keep = step_dir / "quarantine" / self.ref_safe(label)
        moved = []
        for rel in paths:
            # `git clean -nd` reported these relative to the tree it ran in, which
            # is the step's worktree when the step is isolated.
            source = self.tree / rel
            # `.quarantined` is appended deliberately. The quarantine lives inside
            # the repository, and a universal gate like `pytest -q` collects the
            # whole tree regardless of gitignore: a rescued `tests/test_x.py` kept
            # under its own name is collected twice under one module name and fails
            # the very gate the next attempt has to pass. Losing the extension makes
            # the file inert while keeping its path readable.
            destination = keep / (rel + ".quarantined")
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append(rel)
            except (OSError, shutil.Error) as exc:
                self.log(f"    could not quarantine {rel}: {exc}")
        if moved:
            (keep / "MANIFEST.txt").write_text(
                "Untracked files moved here instead of being deleted by `git clean -fd`.\n"
                "Each keeps its original path with `.quarantined` appended; strip that\n"
                "suffix and copy it back to restore it.\n\n" + "\n".join(moved) + "\n",
                encoding="utf-8")
            self.log(f"    QUARANTINED {len(moved)} untracked path(s) to {keep} instead of"
                     f" deleting them: {', '.join(moved[:5])}"
                     + (" ..." if len(moved) > 5 else ""))
        return moved

    def safe_reset(self, base, why, label, step_dir):
        """Reset to `base` - but never destroy a commit this runner did not make.

        The runner does NOT own the branch for the hours a run lasts. The operator,
        another Claude window, an IDE or a hook can commit at any point inside a
        step, and a `reset --hard` to a baseline taken at step entry would remove
        that commit silently - a branch that has quietly lost a commit looks exactly
        like a branch that never had one. So every worker commits under a run-
        specific committer identity (see `worker_identity`), which makes the
        partition of `base..HEAD` into ours and theirs a fact rather than a guess:

          * everything about to be discarded is TAGGED and LOGGED with its subject,
            and listed in the step directory, whoever made it;
          * if ANY commit in the range is foreign, the reset is REFUSED. Refusing is
            a perfectly good outcome; destroying somebody else's work is not.

        Returns "reset", "refused", "nothing" or "no-git".
        """
        if not self.has_git:
            self.log(f"    CANNOT RESET ({why}): {self.no_git_reason}."
                     " The worker's edits stay on disk and must be undone by hand.")
            return "no-git"
        losing = self.commits_since(base)
        if losing:
            record = [f"# commits that the reset for `{label}` would discard ({why})", ""]
            for index, (sha, email, subject) in enumerate(losing, 1):
                tag = f"rescue/{self.ref_safe(label)}/{index}"
                mine = email == self.committer_email
                self.git("tag", "-f", tag, sha)
                self.log(f"    DISCARDING {sha[:8]} [{'worker' if mine else 'FOREIGN'}]"
                         f" {subject[:90]} - kept as tag `{tag}`")
                record.append(f"- `{sha}` {'worker' if mine else 'FOREIGN'} <{email}>"
                              f" tag `{tag}`: {subject}")
            step_dir.mkdir(parents=True, exist_ok=True)
            # APPENDED, never overwritten: a step has up to three attempts and each
            # may discard something. Writing the file fresh each time would leave
            # only the last attempt's losses on disk, which is the same silent
            # disappearance this whole mechanism exists to prevent.
            with (step_dir / "discarded-commits.md").open("a", encoding="utf-8") as handle:
                handle.write("\n".join(record) +
                             "\n\nRecover one with `git cherry-pick <sha>`;"
                             " drop the tags with `git tag -d <tag>` when done.\n\n")
            foreign = [c for c in losing if c[1] != self.committer_email]
            if foreign:
                self.log(f"    REFUSING TO RESET ({why}): {len(foreign)} commit(s) on this"
                         " branch were made by somebody other than this run's workers."
                         " The tree is left as it is; see"
                         f" {step_dir / 'discarded-commits.md'}.")
                for sha, email, subject in foreign:
                    self.log(f"      foreign: {sha[:8]} <{email}> {subject[:90]}")
                return "refused"
        self.quarantine_untracked(step_dir, label)
        self.log(f"    resetting to {base[:8]} ({why})")
        self.git("reset", "--hard", base)
        self.git("clean", "-fd")                 # never -x; ignored dirs hold the run's logs
        return "reset" if losing else "nothing"

    def tree_dirty(self, ignoring=None):
        """Porcelain status, minus untracked files that were already there.

        A run is hours long and the repository is not frozen for any of it. A file
        another window creates DURING a step is not the worker's doing, and on
        2026-09-05 one such file was swept into a worker's commit by `git add -A`
        and cost 37 minutes to an unrelated guard. The worker is now told to commit
        by explicit path and to leave strays alone - so the clean-tree gate must
        not then fail on the stray it was told to leave.
        """
        if not self.has_git:
            return ""                            # nothing is dirty when nothing is tracked
        lines = [ln for ln in self.git("status", "--porcelain")[1].splitlines() if ln.strip()]
        ignoring = ignoring if ignoring is not None else getattr(self, "baseline_untracked", set())
        kept = [ln for ln in lines if not (ln.startswith("??") and ln in ignoring)]
        return "\n".join(kept)

    def snapshot_untracked(self):
        """The `??` lines as a step begins; anything new is the worker's."""
        if not self.has_git:
            self.baseline_untracked = set()
            return self.baseline_untracked
        self.baseline_untracked = {
            ln for ln in self.git("status", "--porcelain")[1].splitlines()
            if ln.startswith("??")}
        return self.baseline_untracked

    # -- gates ---------------------------------------------------------------
    def run_command(self, argv, timeout=1800, shell=False):
        try:
            done = subprocess.run(argv, cwd=self.tree, env=child_env(),
                                  timeout=timeout, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", shell=shell)
        except subprocess.TimeoutExpired:
            return 124, f"TIMEOUT after {timeout}s"
        except FileNotFoundError as exc:
            return 127, f"NOT FOUND: {exc}"
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def check_gate(self, gate, handle):
        name = gate_name(gate)
        if "clean_tree" in gate:
            if not self.has_git:
                handle.write(f"\n--- gate: {name}\n(skipped: {self.no_git_reason})\n")
                return True, ""
            dirty = self.tree_dirty()
            handle.write(f"\n--- gate: {name}\n{dirty or '(clean)'}\n")
            return not dirty, dirty
        if "file" in gate:
            path = self.tree / gate["file"]
            ok = path.exists() and path.stat().st_size > 0
            handle.write(f"\n--- gate: {name}\n    "
                         f"{'present' if ok else 'MISSING OR EMPTY'}\n")
            return ok, "" if ok else f"{gate['file']} missing or empty"
        if "fresh_shell" in gate:
            # "Does it work in a NEW terminal?" - the only honest test of an install,
            # because this process inherited a PATH that may already contain the
            # thing being proved. Windows: rebuild PATH from the registry, which is
            # what a new shell does. POSIX: a LOGIN shell, which re-reads the
            # profile that would set it up.
            if os.name == "nt":
                rebuild = ("$env:Path = [Environment]::GetEnvironmentVariable('Path',"
                           "'Machine') + ';' + [Environment]::GetEnvironmentVariable("
                           "'Path','User'); ")
                argv = ["powershell", "-NoProfile", "-Command", rebuild + gate["fresh_shell"]]
            else:
                argv = [os.environ.get("SHELL") or "/bin/sh", "-lc", gate["fresh_shell"]]
            code, output = self.run_command(argv)
        else:
            command = gate.get("cmd") or gate.get("cmd_empty")
            code, output = self.run_command(command, shell=True)
        ok = code == 0
        if ok and "cmd_empty" in gate and output.strip():
            ok = False
            output += "\n(gate requires EMPTY output)"
        handle.write(f"\n--- gate: {name}\n    exit {code}\n{output}\n")
        handle.flush()
        return ok, output

    @staticmethod
    def failure_label(gate, output):
        """What actually failed - which for clean_tree is the paths, not the name.

        A gate's display name is prose the operator wrote for the brief, and
        clean_tree's reads `tree clean (the worker committed)`. Printed after
        `FAIL ... failed the gate:` it parses as a DIAGNOSIS - "the worker
        committed, and that was the problem" - which is the opposite of the truth,
        and it was misread on its first real failure. The offending paths were
        already in hand one line later, for the quarantine.
        """
        if "clean_tree" not in gate:
            return gate_name(gate)
        paths = [ln.strip() for ln in output.splitlines() if ln.strip()]
        shown = ", ".join(paths[:4]) + (" ..." if len(paths) > 4 else "")
        return (f"clean_tree - {len(paths)} path(s) left uncommitted: {shown}"
                if paths else "clean_tree")

    def run_gates(self, gates, handle):
        for gate in gates:
            ok, output = self.check_gate(gate, handle)
            if not ok:
                return False, self.failure_label(gate, output), output
        return True, "", ""

    def all_gates(self, step):
        return list(step.get("gates") or []) + list(self.run_cfg.get("gates") or [])

    # -- workers -------------------------------------------------------------
    def tier(self, step, kind=None):
        kind = kind or step["kind"]
        defaults = dict(DEFAULT_TIERS.get(kind, DEFAULT_TIERS["build"]))
        defaults.update((self.run_cfg.get("defaults") or {}).get(kind) or {})
        return {"model": step.get("model", defaults["model"]),
                "effort": step.get("effort", defaults["effort"])}

    def budget_for(self, step):
        """The cap for this step's workers - its own if it sets one.

        Time has always been per-step (`timeout_min`); money was not, and one
        run-level cap has to be sized for the plan's LARGEST step, which leaves
        every smaller step effectively uncapped. `budget_usd` on a step is the
        matching knob, and it is what makes the cap mean something per step
        rather than per plan.
        """
        return step.get("budget_usd", self.run_cfg.get("budget_usd_per_step"))

    def worker_argv(self, kind, tier, schema=None, disallow=(), budget=None):
        argv = [self.claude, "-p", "--model", tier["model"], "--effort", tier["effort"],
                "--output-format", "stream-json", "--verbose"]
        # One merged --disallowedTools: the flag takes a list, and passing it
        # twice would leave the runner depending on which of the two the CLI
        # keeps.
        denied = list(UNATTENDED_DENY)
        if kind == "build":
            argv += ["--permission-mode", "bypassPermissions"]
        elif kind == "review":
            argv += ["--permission-mode", "bypassPermissions"]
            denied += ["Edit", "Write", "NotebookEdit"]
        elif kind in ("reflect", "diagnostic"):
            # `acceptEdits` auto-accepts an edit and nothing else, so a shell
            # command waits for an approval that nobody is awake to give and is
            # refused. That made a reflect step trusted to rewrite the plan and
            # commit it, but not to copy a file (2026-09-06, defect 3), and left a
            # diagnostic unable to RUN the failing test it exists to explain.
            # Neither asymmetry is defensible. What actually fences these two is
            # not the permission mode: a reflect's changes outside the plan and the
            # brief directories are reverted, and safe_reset sits under both.
            argv += ["--permission-mode", "bypassPermissions"]
        denied += [name for name in disallow if name not in denied]
        argv += ["--disallowedTools", *denied]
        if budget is None:
            budget = self.run_cfg.get("budget_usd_per_step")
        if budget:
            argv += ["--max-budget-usd", str(budget)]
        if schema:
            argv += ["--json-schema", json.dumps(schema)]
        if self.args.fake_worker:
            argv = [sys.executable, self.args.fake_worker] + argv[1:]
        return argv

    def run_worker(self, argv, brief, log_path, timeout, tag, step_id, step_kind):
        """Spawn a worker, brief on stdin, output streamed to the log, heartbeat
        every minute. Returns (exit code, elapsed seconds, result dict or {})."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        extra = {"OVERNIGHT_STEP_ID": step_id, "OVERNIGHT_STEP_KIND": step_kind,
                 # The tree the worker works in - its own worktree when the step is
                 # isolated. MAIN_REPO is the operator's, for the rare tool that
                 # needs it; a worker must not write there.
                 "OVERNIGHT_REPO": str(self.tree), "OVERNIGHT_OUT": str(self.out),
                 "OVERNIGHT_MAIN_REPO": str(self.repo),
                 "OVERNIGHT_SPEC": str(self.spec_path),
                 # Stamps every commit the worker makes as this run's, so a later
                 # reset can refuse to discard anybody else's. See safe_reset.
                 "GIT_COMMITTER_NAME": f"overnight worker ({self.run_cfg['name']})",
                 "GIT_COMMITTER_EMAIL": self.committer_email}
        with log_path.open("w", encoding="utf-8", errors="replace") as handle:
            handle.write(f"$ {' '.join(argv)}\n\n=== BRIEF ===\n{brief}\n"
                         "=== WORKER OUTPUT ===\n")
            handle.flush()
            started = time.time()
            process = subprocess.Popen(
                argv, cwd=self.tree, env=child_env(extra), stdin=subprocess.PIPE,
                stdout=handle, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace")
            try:
                process.stdin.write(brief)
                process.stdin.close()
            except OSError as exc:
                handle.write(f"\n*** could not write the brief: {exc}\n")
            next_beat = started + 60
            beats = 0
            last_size, last_growth = 0, started
            while True:
                code = process.poll()
                if code is not None:
                    break
                now = time.time()
                if now - started > timeout:
                    kill_tree(process)
                    handle.write(f"\n\n*** KILLED after {timeout}s\n")
                    code = 124
                    break
                # Sampled every pass, not once a beat: the beat is a minute apart,
                # and detection must not be quantised to it.
                handle.flush()
                size = log_path.stat().st_size if log_path.exists() else 0
                if size > last_size:
                    last_size, last_growth = size, now
                if self.stall_min and now - last_growth > self.stall_min * 60:
                    kill_tree(process)
                    handle.write(f"\n\n*** KILLED: STALLED - the log had not grown"
                                 f" for {self.stall_min} min\n")
                    self.log(f"{tag} STALLED - no output at all for"
                             f" {self.stall_min} min. Killing it; the attempt is"
                             f" spent and the retry is the fix.")
                    code = 125
                    break
                if now >= next_beat:
                    beats += 1
                    # Every beat to run.log, every fifth to stdout. Ten minutes was
                    # too coarse to watch by: a build step is planned at about 15,
                    # so a tenth-beat console showed one line before the step was
                    # due to finish and could not distinguish "nearly done" from
                    # "twice its estimate". Five is granular enough to see a step
                    # overrunning while it is still worth reacting to, and the file
                    # keeps its per-minute line either way.
                    self.log(f"{tag} still running, {(now - started) / 60:.0f} min,"
                             f" log {size / 1024:.0f} KB", echo=(beats % 5 == 0))
                    next_beat = now + 60
                time.sleep(2)
            elapsed = time.time() - started
            handle.write(f"\n\n=== exit {code} after {elapsed / 60:.1f} min ===\n")
        result = self.result_of(log_path)
        cost = result.get("total_cost_usd") or 0.0
        # Held per step until the step is recorded; the run's total is then the sum
        # of the ledger and nothing accumulates it separately.
        self.step_cost[step_id] = self.step_cost.get(step_id, 0.0) + cost
        self.count_barren(code, result, tag)
        return code, elapsed, result

    def count_barren(self, code, result, tag):
        """Track consecutive invocations that produced NOTHING. See __init__.

        Barren is a high bar on purpose, and each half of it matters. The worker
        must have FAILED - a worker that exits 0 having decided to do nothing is a
        judgement, not an outage - and it must have produced NO result event: a
        worker that ran, spent money and then hit the wall did real work, and the
        wall it hit is the next invocation's business, not this one's.

        124 and 125 are excluded because they are the runner's OWN kills - the
        timeout and the stall watchdog. A worker that had to be killed is hung, not
        absent, and parking for half an hour would neither diagnose it nor fix it.
        Excluding 125 also stops three stalls in a row from being read as a usage
        wall: a stalled worker leaves no result event and exits non-zero, which is
        the exact shape of a barren one.
        """
        did_something = bool(result.get("total_cost_usd") or result.get("num_turns"))
        if code == 0 or code in (124, 125) or did_something:
            if self.barren:
                self.log(f"{tag} the worker answered; the barren count goes back to 0")
            self.barren = 0
            return
        self.barren += 1
        self.log(f"{tag} the worker returned NOTHING (exit {code}, no result event)"
                 f" - {self.barren} in a row, wall at {self.wall_threshold}")

    def probe(self):
        """Is the account answering at all? The smallest question that can be asked.

        Retrying the real step is the obvious probe and the wrong one: a build
        brief is thousands of characters, it writes a fresh cache prefix, and one
        of those every half hour all night is a bill for nothing. This is haiku,
        no tools, one word of expected output - a rounding error, which is what
        lets the poll be frequent enough to be useful.
        """
        self.probes += 1
        log_path = self.out / "probes" / f"probe-{self.probes:02d}.log"
        argv = [self.claude, "-p", "--model", "haiku",
                "--output-format", "stream-json", "--verbose",
                "--disallowedTools", *UNATTENDED_DENY]
        if self.args.fake_worker:
            argv = [sys.executable, self.args.fake_worker] + argv[1:]
        code, elapsed, result = self.run_worker(
            argv, "Reply with the single word: ok", log_path, 300,
            "    probe:", "_probe", "probe")
        # The probe is not a step and will never be recorded, so its spend would
        # sit in step_cost for ever and never reach the ledger. Taken out here and
        # reported in its own right by write_summary.
        self.probe_cost += self.step_cost.pop("_probe", 0.0)
        alive = code == 0 and bool(result)
        self.log(f"    probe {self.probes}: {'ALIVE' if alive else 'still walled'}"
                 f" (exit {code}, {elapsed:.0f}s)")
        return alive

    def park(self, step_id):
        """Wait for the environment to come back, and say so while waiting.

        A parked run and a hung run look identical from outside, so this logs on a
        cadence throughout: `/overnight progress` and a `tail -f` both keep showing
        something moving.

        PARKED TIME DOES NOT EXTEND THE CLOCK. `--hours` is a promise about when
        the operator will be able to look, not a quantity of compute owed, so a
        long wall eats into the work rather than pushing the run into the morning.
        The cost of that choice is stated in SUMMARY.md rather than hidden: the
        summary says how much of the night went to waiting.

        Returns True if the environment came back and the run should carry on.
        """
        since = time.time()
        self.log("=" * 72)
        self.log(f"PARKED after {step_id}: {self.barren} consecutive workers returned"
                 f" nothing, so nothing can succeed right now. The plan is NOT being"
                 f" marched through - it waits here.")
        self.log(f"    probing every {self.park_poll_min:.0f} min until the account"
                 f" answers. The clock is unchanged: this run still stops starting"
                 f" steps at"
                 f" {dt.datetime.fromtimestamp(self.stop_at):%Y-%m-%d %H:%M}.")
        while True:
            next_probe = time.time() + self.park_poll_min * 60
            while time.time() < next_probe:
                if time.time() > self.stop_at:
                    self.parked_seconds += time.time() - since
                    self.log(f"    the stop time passed while parked."
                             f" {(time.time() - since) / 60:.0f} min parked, "
                             f"{self.probes} probe(s).")
                    return False
                # Five minutes, not thirty: the heartbeat is the whole reason a
                # parked run is distinguishable from a dead one.
                time.sleep(min(300, max(1.0, next_probe - time.time())))
                if time.time() < next_probe:
                    self.log(f"    parked {(time.time() - since) / 60:.0f} min,"
                             f" next probe at"
                             f" {dt.datetime.fromtimestamp(next_probe):%H:%M},"
                             f" {self.probes} probe(s) so far", echo=False)
            if self.probe():
                self.parked_seconds += time.time() - since
                self.barren = 0
                self.log(f"RESUMING: the account answered after"
                         f" {(time.time() - since) / 60:.0f} min parked and"
                         f" {self.probes} probe(s). Picking up at {step_id}.")
                self.log("=" * 72)
                return True

    @staticmethod
    def result_of(log_path):
        """How the worker ended, with its cost and turns summed over the WHOLE log.

        A worker's stdout is not always one session. If it backgrounds a task, its
        `result` fires, the task finishes, the completion wakes the process, and a
        fresh `init` and a second session are appended to the same stream - the
        runner sees one process and one log, but the log holds two results.

        Reading only the last one, as this did, reported the re-woken session's
        figures as the step's: FinKit `5b-recognise` on 2026-09-07 was logged
        `turns=4 $14.53` for a step that actually took 119 turns over two sessions.
        Every per-step turn count in every summary was wrong whenever a worker
        backgrounded anything.

        The fields do NOT all accumulate the same way, and this was measured on
        that log rather than assumed. Session 2 there did a twentieth of session
        1's work (889k cache reads against 18.3M) and yet reported a HIGHER
        `total_cost_usd` - because cost is cumulative over the process, while
        `usage`, `num_turns` and `duration_ms` are per-session. So summing cost
        would have double-counted the step to $28.52. It is taken from the last
        result, which already carries the whole process's spend; turns and
        duration are summed; and the terminal state (subtype, is_error, the
        worker's closing text) comes from the last result too, because that is how
        the process actually ended.
        """
        results = []
        for line in read_text(log_path).splitlines():
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "result":
                results.append(payload)
        if not results:
            return {}
        merged = dict(results[-1])
        if len(results) > 1:
            for field in ("num_turns", "duration_ms"):
                total = sum(r.get(field) or 0 for r in results)
                if total:
                    merged[field] = total
            merged["sessions"] = len(results)
        return merged

    @staticmethod
    def one_line(result):
        if not result:
            return ""
        bits = []
        if result.get("num_turns"):
            bits.append(f"turns={result['num_turns']}")
        if result.get("total_cost_usd"):
            bits.append(f"${result['total_cost_usd']:.2f}")
        text = (result.get("result") or "").strip().replace("\n", " ")
        return " ".join(bits) + (" | " + text[:300] if text else "")

    @staticmethod
    def compact_transcript(log_path, limit=40000):
        """The assistant's own words and every tool error, from a stream-json log.

        This is what a diagnostic or reflect worker reads instead of the raw log:
        3 MB of JSON becomes tens of KB of what was said and what went wrong.
        """
        out = []
        for line in read_text(log_path).splitlines():
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            message = event.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if kind == "assistant" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        out.append("ASSISTANT: " + block["text"].strip())
                    elif block.get("type") == "tool_use":
                        summary = json.dumps(block.get("input", {}))[:200]
                        out.append(f"TOOL {block.get('name')}: {summary}")
            elif kind == "user" and isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(b.get("text", "") for b in body
                                        if isinstance(b, dict))
                    body = str(body or "")
                    if block.get("is_error") or any(
                            w in body for w in ("Traceback", "FAILED", "Error",
                                                "error:", "fatal:")):
                        out.append("TOOL RESULT (error): " + body[:600])
            elif kind == "result":
                out.append("RESULT: " + str(event.get("result", ""))[:1500])
        text = "\n".join(out)
        if len(text) > limit:
            text = text[:limit // 3] + "\n\n...[middle elided]...\n\n" + text[-2 * limit // 3:]
        return text

    # -- briefs --------------------------------------------------------------
    def preamble(self):
        path = self.run_cfg.get("preamble")
        return read_text(self.repo / path) if path else ""

    def gate_text(self, step):
        lines = []
        for gate in self.all_gates(step):
            if "cmd" in gate:
                lines.append(f"- {gate_name(gate)}  ->  `{gate['cmd']}`")
            elif "cmd_empty" in gate:
                lines.append(f"- {gate_name(gate)}  ->  `{gate['cmd_empty']}` (must print nothing)")
            elif "file" in gate:
                lines.append(f"- {gate_name(gate)}  ->  file `{gate['file']}` exists, not empty")
            elif "fresh_shell" in gate:
                lines.append(f"- {gate_name(gate)}  ->  `{gate['fresh_shell']}` in a fresh"
                             " PowerShell with PATH rebuilt from the registry")
            else:
                lines.append("- the working tree is clean: `git status --porcelain` prints nothing")
        return "\n".join(lines)

    def build_brief(self, step, attempt, remediation="", rework=""):
        parts = [self.preamble().replace("{CHUNK}", step["id"]),
                 f"\n# Your step: `{step['id']}`"
                 + (f" - {step['title']}" if step.get("title") else "") + "\n\n",
                 TOOL_USAGE_NOTE,
                 read_text(self.repo / step["brief"]),
                 "\n# The gates your work must pass\n\nThese run after you finish,"
                 " from the repository root. Run them yourself before you commit."
                 " A test node id named here is a CONTRACT: the test must exist"
                 " under exactly that name.\n\n" + self.gate_text(step) + "\n"]
        if remediation:
            parts.append("\n# Remediation plan from the diagnostic pass\n\nTwo attempts"
                         " failed. A diagnostic worker read both and wrote this."
                         " Follow it.\n\n" + remediation + "\n")
        if rework:
            parts.append("\n# REWORK: findings from the review of your previous commit\n\n"
                         "The step is committed and its gates passed. A reviewer found"
                         " the following. Fix it ON TOP of the existing commit (do not"
                         " reset or amend), re-run the gates, commit.\n\n" + rework + "\n")
        tier = self.tier(step, "build")
        parts.append(f"\nThis is attempt {attempt}. You were spawned as"
                     f" {tier['model']}/{tier['effort']} - say so if you write anything"
                     " about which model did this work, because the commit trailer"
                     " your session adds will name whatever your own attribution"
                     " settings name, not what you were spawned as.\n")
        return "".join(parts)

    def handover_brief(self, step, attempt, leg, legs, remediation, rework,
                       previous, work):
        """The brief for a CONTINUATION: the same goal, half-built by somebody else.

        The danger this brief exists to manage is that a fresh worker's instinct
        is to start over. It has none of the previous worker's context, the code
        in front of it is half-finished, and re-deriving it from the brief is the
        path of least resistance - which would spend a second whole cap arriving
        back where the first one stopped. So the state of the tree is stated as
        fact, up front, and the instruction to build on it is explicit.

        The previous worker's own closing words are the handover note. They are
        free - it wrote them before it was cut off - and they are the only
        first-hand account of where it had got to that exists.

        It is built on the SAME brief the cut-off worker had, `remediation` and
        `rework` included. Composing it as a fresh attempt-1 brief instead would
        silently drop both: a continuation of attempt 3 would lose the diagnostic's
        plan, and a continuation of a rework would lose the review's findings -
        so the worker would carry on building the very thing that had just been
        rejected, with nothing in its brief to say so.
        """
        return (self.build_brief(step, attempt, remediation, rework)
                + "\n# CONTINUATION - this step is already part-built\n\n"
                f"You are worker {leg + 1} of at most {legs + 1} on this step. The"
                " worker before you was CUT OFF part-way through, having spent its"
                " whole budget - not because it was wrong, but because it ran out."
                " Its work is still in the tree in front of you.\n\n"
                "**Do not start over.** Read what is there first and build on it."
                " Re-deriving it from the brief would spend your budget arriving"
                " where the last worker already stood. If you find something it did"
                " wrong, fix that thing - do not reset, revert or rewrite what is"
                " sound.\n\n"
                "You are on the same budget it was, so spend it on the part that is"
                " NOT done. Commit as soon as the gates would pass, rather than"
                " polishing: a committed, gate-passing step is the whole objective,"
                " and there may be no worker after you.\n\n"
                "## What is already in the tree\n\n" + (work or "(nothing recorded)")
                + "\n\n## The last worker's own closing words\n\n"
                + (previous.strip()[:2000] or "(it was cut off before it said anything)")
                + "\n")

    def review_brief(self, step, of_step, sha, of_entry):
        return (
            "You are the REVIEW step in an unattended overnight run. Nobody is awake.\n"
            "You are READ-ONLY: Edit and Write are disallowed, and the runner resets the\n"
            "tree after you. Do not commit anything.\n\n"
            + TOOL_USAGE_NOTE + "\n"
            f"Review commit `{sha}` - `git show {sha}` - which delivered step\n"
            f"`{of_step['id']}`. Its gates passed; a gate is a floor, not a standard.\n"
            "Read the brief it was given (below), then the commit, then whatever in the\n"
            "repository you need to judge it. CLAUDE.md is loaded and is the authority.\n\n"
            "Judge, with evidence (file:line, a command's output), against:\n"
            "  1. The brief: did it deliver what was asked, all of it, and nothing that\n"
            "     belongs to another step?\n"
            "  2. The hard rules in CLAUDE.md.\n"
            "  3. Correctness: a test that cannot fail, a guard never shown to bite, a\n"
            "     rule relaxed to make a result come out, a silent default where an\n"
            "     error was specified.\n"
            "  4. Honesty: does the commit message name what it did NOT build?\n\n"
            "Verdict: `pass` (ship it), `rework` (keep the commit, fix the findings on\n"
            "top - write `rework_brief` as instructions a fresh worker can follow), or\n"
            "`fail` (the approach is wrong; say why). Minor findings alone are a pass.\n\n"
            # A reviewer's prose is checked by nothing. On 2026-09-06 a good review
            # said `8 of 41, 33 unhandled` where the truth was 8 of 39 and 31 - it
            # cost nothing there, but a count is the part of a finding an operator
            # acts on without re-deriving, and an estimate that looks like a count
            # is the worst kind of wrong.
            "COUNT, do not estimate. Every number in your findings - how many cases\n"
            "are handled, how many tests changed, how many call sites - must come\n"
            "from a command you ran (`grep -c`, `git show --stat`) and not from\n"
            "reading. Nothing downstream checks your arithmetic.\n\n"
            # From the step's directory, NOT from the ledger: the long summary is
            # deliberately kept out of the yaml to keep the plan scannable, so this
            # is the only place it exists.
            "Worker's own summary of the commit: "
            + (read_text(self.out / dir_name(of_step["id"]) / "summary.md")[:600]
               or of_entry.get("note", "")) + "\n\n"
            f"===== the brief step `{of_step['id']}` was given =====\n"
            + read_text(self.repo / of_step["brief"]) + "\n"
            + (("\n===== extra review guidance =====\n" + read_text(self.repo / step["brief"]))
               if step.get("brief") else ""))

    def reflect_brief(self, step, pending):
        summary = self.summary_table()
        decisions = self.run_cfg.get("decisions_file", DEFAULT_DECISIONS)
        rel_spec = self.spec_path.relative_to(self.repo).as_posix()
        return (
            "You are the REFLECT step in an unattended overnight run. Nobody is awake.\n\n"
            + TOOL_USAGE_NOTE + "\n"
            "Your job: read how the night has gone and decide whether the REMAINING\n"
            f"steps of the plan should change. The plan is `{rel_spec}`; its format is\n"
            "explained below. You may edit it - and add brief files beside the existing\n"
            "ones - subject to these rules, which the runner enforces and reverts on\n"
            "violation:\n"
            "  * a step that has already run (listed under Completed) may not be changed,\n"
            "    removed or reordered;\n"
            "  * pending steps may be edited, removed, reordered, or new ones inserted;\n"
            "  * a new build step needs a brief file, written in the same register as\n"
            "    the existing briefs, pointing at documents rather than restating them;\n"
            "  * the file must stay valid YAML in the same shape;\n"
            "  * do not change `run:`.\n"
            "Do not touch anything else in the repository. Do not commit; the runner\n"
            "commits your change with your rationale.\n\n"
            "Grounds for changing the plan: a step's findings make a later step\n"
            "pointless or wrong; a decision file entry blocks a later step and an\n"
            "interim is possible; a stuck step should be split or dropped; a defect found\n"
            "in passing deserves its own step. Do NOT change the plan to make it easier.\n"
            "If nothing should change, say so and change nothing.\n\n"
            f"===== run so far =====\n{summary}\n\n"
            f"===== pending steps =====\n{yaml.safe_dump(pending, sort_keys=False)}\n"
            f"===== tail of run.log =====\n{read_text(self.out / 'run.log')[-6000:]}\n\n"
            f"===== {decisions} (head) =====\n"
            + read_text(self.repo / decisions, limit=12000) + "\n"
            + (("\n===== extra reflect guidance =====\n" + read_text(self.repo / step["brief"]))
               if step.get("brief") else "")
            + "\n===== spec format =====\n" + SPEC_FORMAT)

    def diagnostic_brief(self, step, step_dir):
        logs = []
        for attempt in (1, 2):
            path = step_dir / f"attempt-{attempt}.log"
            if path.exists():
                logs.append(f"\n===== attempt {attempt}: compact transcript =====\n"
                            + self.compact_transcript(path)
                            + "\n===== attempt {attempt}: gate output =====\n"
                            + self.gate_section(path))
        return (
            "You are the DIAGNOSTIC pass in an unattended overnight run. Two attempts at\n"
            "one step failed and were reset, so the repository is as both started.\n\n"
            + TOOL_USAGE_NOTE + "\n"
            f"Write ONE file and nothing else: `{(step_dir / 'remediation.md').relative_to(self.repo).as_posix()}`\n\n"
            "It must say, with evidence from the transcripts: (1) what went wrong on each\n"
            "attempt, naming the gate and quoting the output; (2) whether they share a\n"
            "root cause; (3) what the third attempt must do DIFFERENTLY, concretely;\n"
            "(4) what it must NOT retry; (5) if the step as briefed is not achievable,\n"
            "the smallest useful subset. Do not edit code. Do not commit.\n\n"
            f"===== the brief =====\n{read_text(self.repo / step['brief'])}\n" + "".join(logs))

    @staticmethod
    def gate_section(log_path):
        text = read_text(log_path)
        marker = "=== GATES ==="
        return text.split(marker, 1)[1][:8000] if marker in text else ""

    # -- the step kinds ------------------------------------------------------
    def run_build(self, step, rework="", base_sha=None):
        """A build step: up to N attempts, diagnostic after the second.
        With `rework`, one attempt on top of `base_sha` (no reset first)."""
        step_dir = self.out / dir_name(step["id"])
        step_dir.mkdir(parents=True, exist_ok=True)
        before = self.head()
        if self.isolation == "worktree" and not self.args.dry_run:
            # The step runs on its own branch, in its own tree, and comes back at
            # the end through integrate(). Nothing it does can touch the operator's
            # tree, so nothing the operator does can fail its gates.
            return self.run_build_isolated(step, step_dir, before, rework, base_sha)
        return self.run_attempts(step, step_dir, before, rework, base_sha)

    def run_build_isolated(self, step, step_dir, before, rework, base_sha):
        """The same attempts, in a worktree, integrated once at the end.

        The operator's branch and working tree are read but never written by the
        worker, never reset and never cleaned. Everything the retry loop does -
        the reset between attempts, the quarantine, the clean-tree gate - now acts
        on a tree nobody else is touching, which is what makes those defences
        unnecessary rather than merely careful.
        """
        base = base_sha or self.head_of(self.repo)
        branch = self.scratch_branch(step["id"])
        if not rework:
            # A crashed run's work is on this branch and nowhere else. Test it
            # before add_worktree force-moves the branch off it. A rework is
            # exempt: the step has already passed once, and what is on the branch
            # is the very thing the reviewer rejected.
            stranded = self.retest_stranded(step, step_dir, base)
            if stranded is not None:
                return stranded
        path = self.add_worktree(step["id"], base)
        if path is None:
            note = "could not create the worktree for this step"
            return {"outcome": "STUCK", "attempts": 0, "note": note}
        self.log(f"[{step['id']}] isolated on `{branch}` at {base[:8]} in {path}")
        outer = self.tree
        try:
            self.tree = path
            result = self.run_attempts(step, step_dir, base, rework, base)
        finally:
            self.tree = outer
        if result["outcome"] != "PASS":
            self.remove_worktree(path)             # the branch holds nothing worth keeping
            return result
        # BEFORE integrating, always: a replay has to check the scratch branch out,
        # and git refuses to check out a branch that another worktree is holding.
        # The commits are on the branch, so the directory has done its job.
        self.remove_worktree(path)
        status, sha = self.integrate(step["id"], base, branch)
        if status == "conflict":
            # NOT discarded, and NOT re-run on resume: the work exists, on a named
            # branch, and only a person can say how it should land.
            note = (f"gates passed in isolation but the work does not replay onto the"
                    f" branch; it is kept on `{branch}`. Merge it by hand"
                    " (`git rebase`/`git cherry-pick`), or drop the branch.")
            self.log(f"[{step['id']}] NEEDS MERGE - {note}")
            result.update({"outcome": "NEEDS MERGE", "sha": "", "note": note})
            return result
        # REPLACE the note, never append to it: run_attempts wrote "committed at
        # <sha>" for the commit as it was in the worktree, and a replay rewrites
        # that sha. A note naming a commit that is not on the branch is worse than
        # no note - it is a sha the morning cannot look up.
        result["sha"] = sha
        result["note"] = (f"{status} onto the branch at {sha[:8]}"
                          if status != "nothing" else "gates pass but nothing was committed")
        return result

    def run_attempts(self, step, step_dir, before, rework, base_sha):
        # A dry run's stub worker writes nothing, so a second attempt is the same
        # no-op against the same gates: it proves nothing already proved and spends
        # a real test suite's runtime again to prove it.
        attempts = 1 if (rework or self.args.dry_run) \
            else int(self.run_cfg.get("attempts", 3))
        timeout = 60 * float(step.get("timeout_min", self.run_cfg.get("worker_timeout_min", 90)))
        tier = self.tier(step, "build")
        cap = self.budget_for(step)
        started = time.time()
        remediation = ""
        note = ""
        workers = 0                # every worker spawned here, continuations included
        handover_words = handover_work = ""
        for attempt in range(1, attempts + 1):
            # Re-read the baseline PER ATTEMPT, not per step. A commit that landed
            # between attempts is then below the baseline and cannot be reset away.
            # This narrows the window; safe_reset closes what is left of it.
            attempt_base = base_sha or self.head()
            if attempt == 3 and (step_dir / "remediation.md").exists():
                remediation = read_text(step_dir / "remediation.md")
                self.log(f"[{step['id']}] attempt 3 ingests the remediation plan")
            # ONE ATTEMPT MAY TAKE SEVERAL WORKERS. The cap is per invocation, so a
            # worker cut off part-way can be CONTINUED: the next one gets a fresh
            # cap and the tree exactly as the last left it, and spends the money on
            # the part that is not done rather than on re-deriving the part that
            # is. That is the whole difference from a retry, which spends the same
            # money on repetition. The tree is carried forward between legs and
            # reset only when the attempt ends, so "a failed attempt leaves no
            # trace" still holds at the level where it matters.
            legs = 0 if self.args.dry_run else self.continuations
            over_budget = False
            why_stopped = ""
            for leg in range(legs + 1):
                if leg:
                    brief = self.handover_brief(step, attempt, leg, legs,
                                                remediation, rework,
                                                handover_words, handover_work)
                    suffix = ("rework" if rework else f"attempt-{attempt}") \
                        + f"-continued-{leg}"
                else:
                    brief = self.build_brief(step, attempt, remediation, rework)
                    suffix = "rework" if rework else f"attempt-{attempt}"
                log_path = step_dir / f"{suffix}.log"
                state_before = self.tree_state()
                if self.args.dry_run:
                    # Said as a stub, not as a worker: "worker exit 0 after 0.0 min"
                    # reads like a worker that ran and found nothing to do.
                    self.log(f"[{step['id']}] {suffix}: (dry run) brief composed for"
                             f" {tier['model']}/{tier['effort']}, {len(brief)} chars ->"
                             f" {log_path.name}. No worker is spawned; the gates run next.")
                    log_path.write_text(f"(dry run)\n\n=== BRIEF ===\n{brief}\n",
                                        encoding="utf-8")
                    code, elapsed, result = 0, 0.0, {}
                else:
                    self.log(f"[{step['id']}] {suffix}: worker starting ({tier['model']}/"
                             f"{tier['effort']}, {len(brief)} chars) -> {log_path.name}")
                    code, elapsed, result = self.run_worker(
                        self.worker_argv("build", tier, budget=cap), brief, log_path,
                        timeout, f"[{step['id']}] {suffix}:", step["id"], "build")
                    workers += 1
                if not self.args.dry_run:
                    self.log(f"[{step['id']}] {suffix}: worker exit {code} after"
                             f" {elapsed / 60:.1f} min"
                             + (" | " + self.one_line(result) if result else ""))
                # Read BEFORE the gates and acted on AFTER them, deliberately. A
                # worker can commit work that passes and only then run out of
                # budget on the tidying up; that step is a PASS and the cap it hit
                # is nobody's business. The trip only decides what happens when the
                # gates fail.
                over_budget = result.get("subtype") == BUDGET_SUBTYPE
                with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write("\n=== GATES ===\n")
                    ok, failed, output = self.run_gates(self.all_gates(step), handle)
                if ok:
                    sha = self.head()
                    moved = sha != before
                    note = (f"committed at {sha[:8]}" if moved
                            else "gates pass but HEAD did not move")
                    if leg:
                        note += f" (over {leg + 1} workers; the budget cut short {leg})"
                    self.log(f"[{step['id']}] PASS ({note})")
                    return {"outcome": "PASS", "attempts": attempt, "sha": sha,
                            "minutes": round((time.time() - started) / 60, 1),
                            "legs": workers if workers > 1 else None, "note": note,
                            "summary": (result.get("result") or "")[:2000]}
                if not over_budget:
                    break
                if leg == legs:
                    why_stopped = (f"and its {legs} continuation(s) were used up"
                                   if legs else "and continuation is off"
                                   " (run.continuations is 0)")
                    break
                # THE RUNAWAY TEST. A worker that spent an entire cap and left the
                # tree byte-identical produced nothing to hand on: continuing would
                # buy a second cap of the same going-nowhere, and handing a fresh
                # worker a half-built wrong thing to finish is worse than stopping.
                # Without git this cannot be told, and there is no undo either, so
                # "cannot tell" is treated as "do not continue".
                after = self.tree_state()
                if state_before is None or after is None or after == state_before:
                    why_stopped = ("and it had changed NOTHING in the tree, so there"
                                   " was nothing to continue from")
                    break
                handover_words = result.get("result") or ""
                handover_work = self.work_since(attempt_base)
                self.log(f"[{step['id']}] {suffix}: cut off with work in the tree -"
                         f" continuing it with a fresh worker ({leg + 2} of"
                         f" {legs + 1} allowed)")
            note = f"failed the gate: {failed}"
            # WHY the attempt failed, not just that a gate did. A worker the runner
            # had to kill never finished its work, so its gate failure says nothing
            # about the code - and the morning must be able to tell that apart from
            # a worker that tried and got it wrong.
            if code == 125:
                note = (f"worker STALLED - silent for {self.stall_min:.0f} min and"
                        f" killed; {note}")
            elif code == 124:
                note = (f"worker TIMED OUT after {timeout / 60:.0f} min and was"
                        f" killed; {note}")
            elif over_budget:
                spent = result.get("total_cost_usd") or 0.0
                note = (f"worker RAN OUT OF BUDGET (${spent:.2f} against a"
                        f" ${float(cap or 0):.2f} cap) {why_stopped}; {note}")
            if self.args.dry_run:
                # No worker ran, so there is nothing to undo - and safe_reset would
                # quarantine the operator's untracked files to get a clean tree,
                # which is precisely the touching a rehearsal must not do.
                self.log(f"[{step['id']}] (dry run) gate not satisfied: {failed}."
                         " Expected - no worker did the work. The tree is untouched.")
                continue
            self.log(f"[{step['id']}] FAIL {suffix} - {note}")
            undo = self.safe_reset(attempt_base, "gate failed",
                                   f"{step['id']}-{suffix}", step_dir)
            if undo == "refused":
                note = ("a third party committed to this branch during the step, so the"
                        " tree was NOT reset; the step is halted rather than rewriting"
                        " somebody else's history. See discarded-commits.md.")
                self.log(f"[{step['id']}] HALTED - {note}")
                return {"outcome": "HALTED", "attempts": attempt, "sha": self.head(),
                        "minutes": round((time.time() - started) / 60, 1), "note": note}
            if over_budget:
                # STOP. Not another attempt and not a diagnostic: both cost a
                # fresh cap to be cut off at the same place, and the diagnostic
                # would be asked to explain a gate failure whose cause is that
                # the worker never got to finish. See OVER_BUDGET.
                note += (f" - NOT retried after {workers} worker(s): the cap is per"
                         " invocation, so another attempt would spend it again for"
                         " the same result. Split the step, raise its `budget_usd`,"
                         " or allow more `run.continuations`.")
                self.log(f"[{step['id']}] {OVER_BUDGET} - {note}")
                return {"outcome": OVER_BUDGET, "attempts": attempt, "sha": self.head(),
                        "minutes": round((time.time() - started) / 60, 1),
                        "legs": workers if workers > 1 else None, "note": note}
            if attempt == 2 and attempts > 2 and not self.args.dry_run:
                self.run_diagnostic(step, step_dir, attempt_base)
        if self.args.dry_run:
            # Not STUCK. Nothing was attempted, so nothing got stuck: the brief
            # composed, the gates ran, and they said what they say against the tree
            # as it stands. Naming that STUCK is what made a rehearsal read like an
            # exhausted step in the morning.
            self.log(f"[{step['id']}] DRY RUN - brief composed, gates ran, {note}")
            return {"outcome": "DRY RUN", "attempts": 1, "note": note,
                    "minutes": round((time.time() - started) / 60, 1)}
        outcome = "REWORK FAILED" if rework else "STUCK"
        if self.barren >= self.wall_threshold:
            # The caller is about to discard this outcome for NOT RUN, so saying
            # STUCK here - let alone "moving on", which is exactly what it will not
            # do - would put a finding about the code in the log two lines above
            # the line explaining that no code was read.
            self.log(f"[{step['id']}] every attempt returned nothing; the wall is called"
                     " below and this outcome is discarded")
        else:
            self.log(f"[{step['id']}] {outcome} after {attempts} attempt(s) - moving on")
        return {"outcome": outcome, "attempts": attempts, "sha": self.head(),
                "minutes": round((time.time() - started) / 60, 1), "note": note}

    def run_diagnostic(self, step, step_dir, before):
        self.log(f"[{step['id']}] running the diagnostic over attempts 1 and 2")
        tier = self.tier(step, "diagnostic")
        brief = self.diagnostic_brief(step, step_dir)
        code, elapsed, result = self.run_worker(
            self.worker_argv("diagnostic", tier), brief, step_dir / "diagnostic.log",
            60 * float(self.run_cfg.get("diagnostic_timeout_min", 20)),
            f"[{step['id']}] diagnostic:", step["id"], "diagnostic")
        wrote = (step_dir / "remediation.md").exists()
        self.log(f"[{step['id']}] diagnostic exit {code} after {elapsed / 60:.1f} min;"
                 f" remediation.md {'written' if wrote else 'NOT written'}")
        self.safe_reset(before, "after diagnostic", f"{step['id']}-diagnostic", step_dir)

    def run_review(self, step):
        of_id = step["of"]
        if not self.has_git:
            note = ("review needs a commit to read and there is none:"
                    f" {self.no_git_reason}")
            self.log(f"[{step['id']}] SKIPPED - {note}")
            return {"outcome": "SKIPPED", "note": note}
        of_entry = self.done_map().get(of_id) or {}
        of_step = next((s for s in self.spec["steps"] if s["id"] == of_id), None)
        if not of_step or of_entry.get("outcome") != "PASS":
            note = f"nothing to review: {of_id} is {of_entry.get('outcome', 'not run')}"
            self.log(f"[{step['id']}] SKIPPED - {note}")
            return {"outcome": "SKIPPED", "note": note}
        sha = of_entry["sha"]
        step_dir = self.out / dir_name(step["id"])
        step_dir.mkdir(parents=True, exist_ok=True)
        tier = self.tier(step, "review")
        brief = self.review_brief(step, of_step, sha, of_entry)
        log_path = step_dir / "review.log"
        self.log(f"[{step['id']}] reviewing {of_id} at {sha[:8]} ({tier['model']}/{tier['effort']})")
        if self.args.dry_run:
            log_path.write_text(f"(dry run)\n\n=== BRIEF ===\n{brief}\n", encoding="utf-8")
            return {"outcome": "SKIPPED", "note": "dry run"}
        code, elapsed, result = self.run_worker(
            self.worker_argv("review", tier, schema=REVIEW_SCHEMA), brief, log_path,
            60 * float(step.get("timeout_min", self.run_cfg.get("review_timeout_min", 30))),
            f"[{step['id']}]", step["id"], "review")
        # The reviewer is read-only; this is belt and braces for the case where it
        # edited something anyway. Through safe_reset, not a bare reset: the tidy-up
        # after the most read-only step in the run was the one place a `git clean
        # -fd` could delete an untracked file of the operator's with no quarantine
        # and no record. Resetting to HEAD discards no commit, so all safe_reset
        # adds here is that protection.
        self.safe_reset(self.head(), "after review", f"{step['id']}-after-review", step_dir)
        verdict = result.get("structured_output") or {}
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("pass", "rework", "fail"):
            note = f"INCONCLUSIVE: no typed verdict (exit {code})"
            self.log(f"[{step['id']}] {note}")
            return {"outcome": "INCONCLUSIVE", "note": note, "minutes": round(elapsed / 60, 1)}
        (step_dir / "verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        blockers = sum(1 for f in verdict.get("findings", []) if f.get("severity") == "blocker")
        majors = sum(1 for f in verdict.get("findings", []) if f.get("severity") == "major")
        self.log(f"[{step['id']}] verdict {verdict['verdict'].upper()} - {blockers} blocker,"
                 f" {majors} major | {verdict.get('summary', '')[:200]}")
        entry = {"outcome": f"REVIEW {verdict['verdict'].upper()}", "of": of_id,
                 "minutes": round(elapsed / 60, 1), "findings": len(verdict.get("findings", [])),
                 "note": verdict.get("summary", "")[:300]}
        if verdict["verdict"] == "pass" or step["on_fail"] == "record":
            return entry
        if step["on_fail"] == "revert":
            base = self.git("rev-parse", f"{sha}^")[1]
            undo = self.safe_reset(base, "review verdict + on_fail: revert",
                                   f"{of_id}-reverted-by-review", step_dir)
            if undo == "refused":
                entry["note"] = ("NOT reverted: a third party has committed on top of the"
                                 " reviewed commit. " + entry["note"])
                self.log(f"[{step['id']}] revert refused - {entry['note'][:200]}")
                return entry
            self.update_done(of_id, {"outcome": "REVERTED BY REVIEW",
                                     "note": f"reverted by {step['id']}"},
                             why=f"REVERTED BY REVIEW ({step['id']})")
            entry["note"] = "reverted " + sha[:8] + ": " + entry["note"]
            return entry
        # on_fail: rework - one build attempt on top of the reviewed commit
        rework = verdict.get("rework_brief") or "\n".join(
            f"- [{f['severity']}] {f['where']}: {f['what']} -> {f['fix']}"
            for f in verdict.get("findings", []))
        self.log(f"[{step['id']}] rework of {of_id} starting")
        outcome = self.run_build(of_step, rework=rework, base_sha=sha)
        # run_build charges its workers to the step it built - but that step was
        # recorded long ago and its step_cost popped with it, so the rework's
        # spend would sit in the dict until the process exited and never reach the
        # ledger or the run total. It belongs to the review that ordered it: that
        # is what makes it visible as overhead rather than as part of the build it
        # is repairing. Moved whether the rework passed or failed - a failed one
        # cost the same.
        self.step_cost[step["id"]] = (self.step_cost.get(step["id"], 0.0)
                                      + self.step_cost.pop(of_id, 0.0))
        if outcome["outcome"] == "PASS":
            self.update_done(of_id, {"sha": outcome["sha"], "reworked": True,
                                     "note": "reworked after review"},
                             why=f"REWORKED after {step['id']}")
            entry["outcome"] = "REVIEW REWORK PASS"
        else:
            entry["outcome"] = "REVIEW REWORK FAILED"
            entry["note"] = "rework failed its gates; the reviewed commit stands. " + entry["note"]
        return entry

    def run_reflect(self, step):
        step_dir = self.out / dir_name(step["id"])
        step_dir.mkdir(parents=True, exist_ok=True)
        done_ids = set(self.done_map())
        pending = [s for s in self.spec["steps"] if s["id"] not in done_ids and s["id"] != step["id"]]
        backup = step_dir / "steps.before.yaml"
        shutil.copyfile(self.spec_path, backup)
        before = self.head()
        tier = self.tier(step, "reflect")
        brief = self.reflect_brief(step, pending)
        self.log(f"[{step['id']}] reflecting over {len(done_ids)} done, {len(pending)} pending"
                 f" ({tier['model']}/{tier['effort']})")
        if self.args.dry_run:
            (step_dir / "reflect.log").write_text(f"(dry run)\n\n=== BRIEF ===\n{brief}\n",
                                                  encoding="utf-8")
            return {"outcome": "SKIPPED", "note": "dry run"}
        code, elapsed, result = self.run_worker(
            self.worker_argv("reflect", tier, schema=REFLECT_SCHEMA), brief,
            step_dir / "reflect.log",
            60 * float(step.get("timeout_min", self.run_cfg.get("reflect_timeout_min", 30))),
            f"[{step['id']}]", step["id"], "reflect")
        verdict = result.get("structured_output") or {}
        rationale = (verdict.get("rationale") or "")[:400] if isinstance(verdict, dict) else ""
        # Validate what it did to the plan.
        try:
            new_spec = load_spec(self.spec_path)
            # The backup taken above IS the fingerprint map: a step that has run is
            # one carrying `done:`, and none of them may differ in any way that
            # matters - including its recorded outcome, which a reflect must never
            # rewrite.
            old_spec = load_spec(backup)
            old_ran = [s for s in old_spec["steps"] if s.get("done")]
            new_by_id = {s["id"]: s for s in new_spec["steps"]}
            for old_step in old_ran:
                sid = old_step["id"]
                match = new_by_id.get(sid)
                if match is None:
                    raise SpecError(f"completed step {sid!r} was changed or removed")
                if completed_shape(match) != completed_shape(old_step):
                    raise SpecError(f"completed step {sid!r} was changed or removed")
            ran_ids = [s["id"] for s in old_ran]
            if [s["id"] for s in new_spec["steps"] if s["id"] in set(ran_ids)] != ran_ids:
                raise SpecError("completed steps were reordered")
            if (new_spec.get("run") or {}) != self.run_cfg:
                raise SpecError("run: section was changed")
            for s in new_spec["steps"]:
                if s["kind"] == "build" and not (self.repo / s["brief"]).exists():
                    raise SpecError(f"step {s['id']!r} names a brief that does not exist: {s['brief']}")
        except SpecError as exc:
            shutil.copyfile(backup, self.spec_path)
            self.safe_reset(before, "reflect produced an invalid plan", f"{step['id']}-invalid", step_dir)
            note = f"REVERTED: {exc}"
            self.log(f"[{step['id']}] {note}")
            return {"outcome": "REFLECT REVERTED", "note": note, "minutes": round(elapsed / 60, 1)}
        changed = read_text(backup) != read_text(self.spec_path)
        if not changed:
            self.safe_reset(before, "reflect changed nothing in the plan", f"{step['id']}-nochange", step_dir)
            self.log(f"[{step['id']}] plan unchanged | {rationale[:200]}")
            return {"outcome": "REFLECT NO CHANGE", "note": rationale, "minutes": round(elapsed / 60, 1)}
        if not self.has_git:
            self.spec = new_spec
            self.log(f"[{step['id']}] PLAN CHANGED (uncommitted: {self.no_git_reason})"
                     f" | {rationale[:200]}")
            return {"outcome": "REFLECT CHANGED", "note": rationale, "sha": self.head(),
                    "added": verdict.get("added"), "removed": verdict.get("removed"),
                    "minutes": round(elapsed / 60, 1)}
        # Commit the plan change (and any new briefs) so the tree is clean.
        rel_spec = self.spec_path.relative_to(self.repo).as_posix()
        brief_dirs = sorted({Path(s["brief"]).parent.as_posix() for s in new_spec["steps"]
                             if s.get("brief")})
        self.git("add", rel_spec, *brief_dirs)
        dirty_other = [l for l in self.tree_dirty().splitlines()
                       if not l.startswith(("A ", "M ", "R ")) or
                       not any(l[3:].startswith(p) for p in [rel_spec] + brief_dirs)]
        if dirty_other:
            self.log(f"[{step['id']}] reflect touched files outside the plan; reverting: {dirty_other[:3]}")
            shutil.copyfile(backup, self.spec_path)
            self.safe_reset(before, "reflect touched files outside the plan", f"{step['id']}-outside", step_dir)
            return {"outcome": "REFLECT REVERTED", "note": "touched files outside the plan",
                    "minutes": round(elapsed / 60, 1)}
        message = (f"overnight: {step['id']} rewrote the plan\n\n{verdict.get('rationale', '')}\n\n"
                   f"added: {', '.join(verdict.get('added') or []) or 'none'}\n"
                   f"removed: {', '.join(verdict.get('removed') or []) or 'none'}\n")
        (step_dir / "commit-msg.txt").write_text(message, encoding="utf-8")
        self.git("commit", "-q", "-F", str(step_dir / "commit-msg.txt"))
        self.spec = new_spec
        self.log(f"[{step['id']}] PLAN CHANGED and committed at {self.head()[:8]} | {rationale[:200]}")
        return {"outcome": "REFLECT CHANGED", "note": rationale, "sha": self.head(),
                "added": verdict.get("added"), "removed": verdict.get("removed"),
                "minutes": round(elapsed / 60, 1)}

    def run_gate_step(self, step):
        step_dir = self.out / dir_name(step["id"])
        step_dir.mkdir(parents=True, exist_ok=True)
        with (step_dir / "gates.log").open("w", encoding="utf-8") as handle:
            ok, failed, output = self.run_gates(step.get("gates") or [], handle)
        outcome = "PASS" if ok else "FAIL"
        self.log(f"[{step['id']}] gate step {outcome}" + (f" - {failed}" if failed else ""))
        return {"outcome": outcome, "note": failed}

    # -- reporting -----------------------------------------------------------
    def summary_table(self):
        rows = ["| step | kind | outcome | min | est | note |",
                "|---|---|---|---|---|---|"]
        for step in self.spec["steps"]:
            e = step.get("done")
            if not e:
                continue
            sid = step["id"]
            e = dict(e, kind=step["kind"])
            est = step.get("expected_min")
            actual = e.get("minutes", 0)
            if est:
                ratio = (actual / est) if est else 0
                est_cell = f"{est:.0f} ({ratio:.1f}x)" + (" **over**" if ratio > 1.5 else "")
            else:
                est_cell = "-"
            rows.append(f"| `{sid}` | {e.get('kind', '')} | **{e['outcome']}** | "
                        f"{actual:.0f} | {est_cell} | {str(e.get('note', ''))[:120]} |")
        rows.append("\nCumulative worker cost over every step this plan has run,"
                    f" not just this session (self-reported): ${self.cost_so_far():.2f}")
        rows.append("\n" + self.overhead_line())
        return "\n".join(rows)

    def overhead_line(self):
        """What the run spent on not-building, in one sentence.

        The efficiency bar (project CLAUDE.md) is that an unattended run must not
        cost far more than the same work done interactively, and the measurement
        behind it found the risk is STRUCTURAL, not per-call: review, reflect,
        rework and discarded attempts were 26% of the first real run. That is a
        number the operator should see every morning, not one a document asserts.

        Two halves, and they are not equally exact. The KIND split is exact: a
        review or a reflect is a whole step with its own ledger entry, and a
        rework's cost is moved onto the review that ordered it (run_review). The
        retried/reworked COUNTS are indicative only - the ledger keeps one cost
        per step, so a build step's discarded attempts are inside its own figure
        and cannot be separated from the attempt that worked.
        """
        build = overhead = 0.0
        retried = reworked = 0
        for step in self.spec["steps"]:
            entry = step.get("done")
            if not entry:
                continue
            cost = float(entry.get("cost_usd") or 0)
            if step["kind"] == "build":
                build += cost
                retried += 1 if (entry.get("attempts") or 1) > 1 else 0
                reworked += 1 if entry.get("reworked") else 0
            else:
                overhead += cost
        total = build + overhead
        if not total:
            return "No worker cost recorded, so no overhead split."
        share = overhead / total * 100
        return (f"Of that, **${overhead:.2f} ({share:.0f}%) was not building**:"
                f" review and reflect steps, and the rework they ordered. Build"
                f" steps ${build:.2f}, of which {retried} needed more than one"
                f" attempt and {reworked} were reworked after a review - that"
                " spend is inside the build figure, because the ledger keeps one"
                " cost per step.")

    def write_summary(self, reason):
        stopped = dt.datetime.now()
        lines = [f"# Overnight run `{self.run_cfg['name']}`", "",
                 f"Started {self.started:%Y-%m-%d %H:%M}, finished {stopped:%Y-%m-%d %H:%M}"
                 f" ({(stopped - self.started).total_seconds() / 3600:.1f} h). Ended: {reason}.",
                 f"HEAD `{self.head()[:8]}`.", ""]
        if self.args.dry_run:
            lines += ["> **A DRY RUN. No worker ran, nothing was recorded in the plan,"
                      " nothing was committed.** The outcomes below are whatever the"
                      " plan already carried, not this rehearsal's.", ""]
        if not self.has_git:
            lines += ["> **" + NO_GIT_NOTICE.format(reason=self.no_git_reason) + "**", ""]
        # SEPARATE from the parked note below, and it took the self-test to see
        # why: under `--on-wall stop` nothing is ever parked, so a summary whose
        # only explanation of NOT RUN hung off the parked block left the outcome
        # standing in the table with nothing saying it was the environment.
        if any((s.get("done") or {}).get("outcome") == NOT_RUN for s in self.spec["steps"]):
            lines += [f"> **A step below is `{NOT_RUN}`.** The workers stopped answering"
                      " entirely - a usage limit, a logged-out CLI, a withdrawn model,"
                      " no network - so it was never really attempted. It is pending,"
                      " not failed, and NOTHING in this run is a finding about its"
                      " code. Relaunch when the account is back and it is picked up"
                      " where it stopped.", ""]
        if self.parked_seconds or self.probes:
            # Said out loud, because otherwise every per-hour figure in this file
            # lies about what the run cost: the hours are wall-clock and some of
            # them bought nothing. Parked time never extended the clock, so this
            # is time the plan lost, not time it was given.
            hours = self.parked_seconds / 3600
            lines += [f"> **{hours:.1f} h of this run was PARKED**, waiting for the"
                      f" environment to come back, over {self.probes} probe(s)"
                      f" costing ${self.probe_cost:.2f}. That time was not work and"
                      " did not extend the stop time, so the per-hour figures below"
                      " are hours, not effort.", ""]
        lines += [self.summary_table(), "",
                 "## Read next", "",
                 f"- `{self.out.relative_to(self.repo).as_posix()}/run.log`, then each step's directory.",
                 f"- `{self.run_cfg.get('decisions_file', DEFAULT_DECISIONS)}`.",
                 "- Every `verdict.json` under a review step, and every `remediation.md`.",
                 "- The commits themselves: a gate is a floor, not a standard.", ""]
        (self.out / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
        self.log(f"summary written to {self.out / 'SUMMARY.md'}")

    # -- the loop ------------------------------------------------------------
    def preflight(self):
        if not self.has_git:
            # Noted ONCE, at the top of the run, and never again: the operator was
            # told loudly at plan time and does not need it beside every step.
            self.log(NO_GIT_NOTICE.format(reason=self.no_git_reason))
        dirty = self.tree_dirty()
        if dirty:
            self.log("REFUSING TO START: working tree is dirty; git reset --hard is this run's"
                     " undo and it would destroy this:")
            for line in dirty.splitlines():
                self.log("    " + line)
            raise SystemExit(2)
        with (self.out / "preflight.log").open("w", encoding="utf-8") as handle:
            ok, failed, output = self.run_gates(self.run_cfg.get("gates") or [], handle)
        if not ok:
            self.log(f"REFUSING TO START: universal gate fails before any step: {failed}")
            self.log(output[-2000:])
            raise SystemExit(2)
        self.log("preflight: tree clean, universal gates pass")
        if getattr(self, "_isolation_note", ""):
            self.log(f"    {self._isolation_note}")
        if self.isolation == "worktree":
            if self.args.dry_run:
                # A rehearsal does not isolate (see run_build_step), so probing
                # would be a write made for a run that is not going to happen -
                # the same shape as the --list that invented a run in flight.
                self.log("    (dry run: isolation is not exercised and not probed)")
            else:
                self.prune_worktrees()
                self.preflight_worktree()

    def preflight_worktree(self):
        """Run the universal gates in a THROWAWAY worktree as well as in the tree.

        The one thing that makes isolation dangerous is what git does not carry: a
        fresh checkout has no .venv, no node_modules, no local .env. Gates that
        pass for the operator fail there for reasons nothing to do with any worker
        - and the runner would call that a failed attempt, spend three of them, and
        report STUCK in the morning. One worktree and one gate run, here, buys the
        whole night. Detached, so no branch is created and nothing is left behind.
        """
        gates = self.run_cfg.get("gates") or []
        # The probe is made even with no universal gates to run in it. Isolation is
        # the DEFAULT now, so "can this project make a worktree at all?" is a
        # question every run has to answer, and the honest place to answer it is
        # here rather than in the first build step of the night.
        probe = self.add_worktree("_probe", self.head_of(self.repo), detach=True)
        if probe is None:
            self.log("REFUSING TO START: isolation is on but a worktree cannot be made.")
            self.log("    Set `run.isolation: in-place` if this project cannot use one.")
            raise SystemExit(2)
        ok, failed, output = True, "", ""
        outer = self.tree
        try:
            self.tree = probe
            if gates:
                with (self.out / "preflight-worktree.log").open("w", encoding="utf-8") as handle:
                    ok, failed, output = self.run_gates(gates, handle)
        finally:
            self.tree = outer
            self.remove_worktree(probe)
        if not ok:
            # THIS MESSAGE HAS TO TEACH. Isolation is the default, so a project
            # that has never been cloned meets this refusal without having asked
            # for anything, and the natural response - set `in-place` and move on -
            # is the one that costs it the protection isolation exists to give.
            # The failures are usually REAL: a suite that only passes in the
            # operator's working copy has been hiding something. FinKit found two
            # such defects, weeks old, the first time it was probed this way.
            self.log("REFUSING TO START: your gates pass in your working copy but"
                     f" FAIL in a fresh worktree: {failed}.")
            self.log("    Isolation raises the bar from `the suite passes here` to"
                     " `the suite passes from a clone`, and this suite does not"
                     " clear it yet. Usual causes, in the order they turn up:")
            self.log("      * a test reads a GENERATED or GITIGNORED artefact that"
                     " no clean checkout has (a built report, a cached download);")
            self.log("      * a fixture git CONVERTS on checkout - line endings,"
                     " `text` attributes - so it is not byte-identical there;")
            self.log("      * the gates need state git does not carry at all: a"
                     " virtualenv, node_modules, a local .env, a build cache.")
            self.log("    In that order, the answers are: FIX THE SUITE (the first"
                     " two are usually real defects your working copy was hiding);"
                     " or list the paths under `run.worktree_link:`, which links"
                     " them into every worktree.")
            self.log("    `run.isolation: in-place` is the fallback and NOT the"
                     " first answer: it puts the workers back in your tree, where a"
                     " file you create mid-step fails their gates and costs a step.")
            self.log(output[-2000:])
            raise SystemExit(2)
        self.log("preflight: a worktree can be made" +
                 (" and the universal gates pass in it too" if gates else
                  " (no universal gates to run in it)"))

    def select(self):
        steps = self.spec["steps"]
        if self.args.only:
            wanted = [s.strip() for s in self.args.only.split(",") if s.strip()]
            unknown = set(wanted) - {s["id"] for s in steps}
            if unknown:
                raise SystemExit(f"unknown step(s): {sorted(unknown)}")
            return [s for s in steps if s["id"] in wanted]
        if self.args.from_step:
            ids = [s["id"] for s in steps]
            if self.args.from_step not in ids:
                raise SystemExit(f"unknown step: {self.args.from_step}")
            return steps[ids.index(self.args.from_step):]
        return steps

    def main(self):
        # Only a launch needs a worker. The exclusive actions - --reset-state,
        # --list, --print-brief - read and write the plan file and stop.
        if not self.claude:
            sys.exit("claude not found on PATH")
        self.out.mkdir(parents=True, exist_ok=True)
        self.acquire_lock()
        self.log("=" * 72)
        self.log(f"run `{self.run_cfg['name']}`: spec {self.spec_path.name}, repo {self.repo},"
                 f" dry_run={self.args.dry_run},"
                 f" fake_worker={bool(self.args.fake_worker)}")
        self.log(f"    the clock: {self.clock_source} - no step is STARTED after"
                 f" {dt.datetime.fromtimestamp(self.stop_at):%Y-%m-%d %H:%M}"
                 f" ({max(0.0, self.hours):.1f} h from now). A step already running is never"
                 f" interrupted by it.")
        self.log(f"    if {self.wall_threshold} workers in a row return nothing:"
                 + (f" PARK and probe every {self.park_poll_min:.0f} min"
                    if self.on_wall == "park" else " STOP, leaving the rest pending"))
        self.preflight()
        reason = "the queue finished"
        session = {}
        index = 0
        while True:
            # Re-read the spec every iteration: a reflect step may have rewritten it.
            try:
                self.spec = load_spec(self.spec_path)
            except SpecError as exc:
                self.log(f"STOP: the spec is no longer valid: {exc}")
                reason = "invalid spec"
                break
            queue = self.select()
            # A step is re-run on resume only if it did not complete: a STUCK or
            # failed build, an inconclusive review, or a review skipped because
            # its subject had not passed. A review or reflect that ran is done.
            todo = [s for s in queue if self.args.rerun or is_resumable(s)]
            todo = [s for s in todo if s["id"] not in self._ran_this_session]
            if not todo:
                break
            step = todo[0]
            if time.time() > self.stop_at:
                reason = "the clock: no step was started after the stop time"
                self.log(f"STOP: {reason}"
                         f" ({dt.datetime.fromtimestamp(self.stop_at):%Y-%m-%d %H:%M},"
                         f" set by {self.clock_source})."
                         f" {len(todo)} step(s) not started:"
                         f" {', '.join(s['id'] for s in todo)}")
                break
            self._ran_this_session.add(step["id"])
            strays = self.snapshot_untracked()
            if strays:
                self.log(f"    {len(strays)} untracked file(s) present before this step;"
                         " the clean-tree gate will ignore them")
            self.log(f"[{step['id']}] start ({step['kind']}) at {self.head()[:8]}"
                     + (f" - {step['title']}" if step.get("title") else ""))
            if step["kind"] == "build":
                outcome = self.run_build(step)
            elif step["kind"] == "review":
                outcome = self.run_review(step)
            elif step["kind"] == "reflect":
                outcome = self.run_reflect(step)
            else:
                outcome = self.run_gate_step(step)
            if self.barren >= self.wall_threshold:
                # THE OUTCOME IS DISCARDED, and that is the point. This step's
                # workers returned nothing, so `STUCK` here would be a finding
                # about code nobody read - the exact thing that made the morning of
                # 2026-09-07 unreadable. It is recorded as NOT RUN, which a resume
                # picks straight back up, and the wall is dealt with below.
                note = WALL_NOTE.format(n=self.barren)
                self.log(f"[{step['id']}] {NOT_RUN} - {note}")
                self.record(step, NOT_RUN, note=note,
                            minutes=outcome.get("minutes"), attempts=outcome.get("attempts"))
                session[step["id"]] = NOT_RUN
                index += 1
                if self.on_wall == "stop" or not self.park(step["id"]):
                    todo = [s for s in todo if s["id"] != step["id"]]
                    reason = (f"the environment stopped answering; {len(todo)} step(s)"
                              " were left to run")
                    self.log(f"STOP: {reason}: {', '.join(s['id'] for s in todo)}")
                    self.log("    They are PENDING, not failed. Relaunch when the"
                             " account is back and the run picks up where it stopped.")
                    break
                # Parked, probed, and the account came back. The step was never
                # really attempted, so it goes back in the queue rather than being
                # skipped as already-run this session.
                self._ran_this_session.discard(step["id"])
                continue
            if step["kind"] != "gate":
                # RECORDED BY THE RUNNER, because the commit cannot be trusted for
                # it: a step spawned sonnet/medium produced a commit trailer naming
                # Opus, the child session's own attribution settings having nothing
                # to do with the --model it was passed. The runner is the only
                # party that knows what it asked for.
                tier = self.tier(step)
                outcome.setdefault("tier", f"{tier['model']}/{tier['effort']}")
            self.record(step, **outcome)
            session[step["id"]] = outcome["outcome"]
            index += 1
            if outcome["outcome"] == "NEEDS MERGE":
                # Unlike a HALTED step, this one STOPS the run. HALTED discards the
                # step's work, so the tree the remaining steps build on is still the
                # tree the plan assumed. NEEDS MERGE means work exists that the tree
                # does NOT have, so every later step would build on a tree the plan
                # did not intend, and the night would be spent on a wrong premise.
                reason = f"{step['id']} could not be merged; a person must land it"
                self.log(f"STOP: {reason}.")
                break
        self.write_summary(reason)
        if self.args.dry_run:
            # The ledger is untouched, so it cannot be the report: what this
            # rehearsal did is what happened in THIS session, and none of it was
            # written down.
            self.log("dry run finished: "
                     + ", ".join(f"{k}={v}" for k, v in session.items()))
            self.log("nothing was recorded in the plan and nothing was committed."
                     " Relaunch without --dry-run to run it for real.")
            self.log.close()
            return 0
        outcomes = {sid: e["outcome"] for sid, e in self.done_map().items()}
        # The TOTAL, on the last line. Per-step cost was already in the log and the
        # sum already in SUMMARY.md, but the number that decides whether to launch
        # the next stretch was not where the morning actually looks first.
        # BOTH numbers, each said to be what it is. `across N recorded step(s)` was
        # the ledger's total - every step the plan has ever run - under wording that
        # read as this session's, which is the one that decides whether to launch
        # the next stretch.
        self.log(f"run finished (this session: ${self.session_cost:.2f} over"
                 f" {len(session)} step(s) | whole plan to date:"
                 f" ${self.cost_so_far():.2f} over {len(outcomes)} recorded): "
                 + ", ".join(f"{k}={v}" for k, v in outcomes.items()))
        self.log.close()
        bad = [v for v in outcomes.values()
               if v not in ("PASS", "REVIEW PASS", "REVIEW REWORK PASS", "REFLECT NO CHANGE",
                            "REFLECT CHANGED", "SKIPPED")]
        return 1 if bad else 0


SPEC_FORMAT = """\
run:
  name: <run name; output goes to overnight/runs/<name>/>
  until: "07:30"                 # stop STARTING steps at this time. `HH:MM` is
                                 # the next occurrence of it, so `07:30` typed
                                 # at 23:00 means tomorrow morning; or give
                                 # `YYYY-MM-DD HH:MM` for one exact moment.
                                 # Preferred over `hours` - it survives a delay
                                 # between writing the plan and launching it
  hours: 6                       # stop STARTING steps after this many hours.
                                 # Superseded by `until`; kept for old plans
  attempts: 3                    # build attempts before STUCK
  worker_timeout_min: 90
  budget_usd_per_step: 40        # optional, --max-budget-usd on every worker.
                                 # A build attempt that trips it is OVER BUDGET
                                 # and is NOT retried - the cap is per worker,
                                 # so retrying spends it again for the same cut
  on_wall: park                  # when N workers in a row return NOTHING - a
                                 # usage limit, a logged-out CLI, no network -
                                 # the run stops marching through the plan.
                                 # `park` (the default) waits and probes cheaply
                                 # until the account answers, then carries on;
                                 # `stop` ends the run there. Either way the
                                 # untested steps are left PENDING, never STUCK.
  wall_threshold: 3              # barren workers in a row before that happens
  park_poll_min: 30              # minutes between probes while parked
  stall_min: 10                  # kill a worker silent this long (0 = off)
  continuations: 0               # extra workers ONE ATTEMPT may use when the cap
                                 # cuts one off part-way. The next gets a fresh
                                 # cap and the tree as the last left it, so the
                                 # money buys progress rather than the repetition
                                 # a retry buys. A worker that changed NOTHING is
                                 # never continued - that is a runaway, not a big
                                 # step. 0 (off) unless the cap is sized per step
  isolation: worktree            # THE DEFAULT. Each build step gets its own git
                                 # worktree on a scratch branch, integrated once
                                 # its gates pass; the operator's tree is never
                                 # written to, reset or cleaned, so a file made
                                 # in it mid-step cannot fail a worker's gate.
                                 # `in-place` is the opt-out, for a project whose
                                 # gates need state git does not carry.
  worktree_link: [.venv]         # paths git does not carry, linked into each
                                 # worktree. Preflight refuses to start if the
                                 # universal gates pass in your tree but fail in
                                 # a fresh worktree, which is what a missing one
                                 # looks like.
  worktree_root: <dir>           # default: <repo>.overnight-worktrees, a SIBLING
                                 # of the repo - never inside it, or `pytest -q`
                                 # collects every test file twice
  preamble: <path to a brief preamble file; {CHUNK} is replaced by the step id>
  decisions_file: overnight/DECISIONS-PENDING.md
  defaults:                      # tier per kind; OM build, OH review/reflect/diagnostic
    build: {model: opus, effort: medium}
  gates:                         # universal, appended to every build step's own
    - {name: ..., cmd: <shell command, exit 0>}
    - {clean_tree: true}
steps:
  - id: <unique>                 # kind: build | review | reflect | gate
    kind: build
    title: <one line>
    brief: <path to brief file>
    model: sonnet                # optional per-step tier override
    effort: medium
    timeout_min: 60
    budget_usd: 6                # this step's own cap, overriding
                                 # run.budget_usd_per_step. Time has always been
                                 # per-step; one run-level cap has to be sized for
                                 # the plan's LARGEST step, which leaves every
                                 # smaller one effectively uncapped
    gates:                       # this step's own; a test NODE ID is a contract
      - {cmd: python -m pytest -q tests/x.py::test_the_claim}
      - {file: docs/09.md}
      - {cmd_empty: git status --porcelain}
      - {fresh_shell: mytool --version}
  - id: review:<step>
    kind: review
    of: <step id>                # must be an earlier step
    on_fail: rework              # record | rework | revert
    brief: <optional extra guidance file>
  - id: reflect-1
    kind: reflect                # may rewrite PENDING steps; runner validates and commits
    brief: <optional extra guidance file>

THE LEDGER. There is no state.json: after every step the runner splices the
outcome into that step's own block and commits the file, so the plan IS the
record of the run. It is spliced textually and verified - comments, key order and
quoting all survive, and an edit that would change another step is refused.

    done:
      outcome: PASS              # the same vocabulary as the log
      at: "2026-09-05 10:22"
      sha: f756c08a              # absent for SKIPPED and for a run without git
      attempts: 1                # build steps
      minutes: 7.7               # wall clock over every attempt
      cost_usd: 2.39             # this step's workers, self-reported
      tier: opus/medium          # what the runner ASKED for. The commit trailer
                                 # cannot be trusted for this: it names whatever
                                 # the child session's own attribution names, not
                                 # the --model the runner passed.
      note: committed at f756c08a
      reworked: true             # after a review rework PASS; sha is updated
      of: <step>  findings: 3    # review steps
      added: [..]  removed: [..] # a reflect that changed the plan

A step is COMPLETE iff its `done.outcome` is outside STUCK, HALTED, FAIL,
INCONCLUSIVE, SKIPPED, REWORK FAILED, REVERTED BY REVIEW, NOT RUN and
OVER BUDGET; anything else is re-run on resume. `--reset-state` strips every
`done:` and commits that.
`--mode` reads the plan alone and prints BLOCKED, PLAN, RUN or REPLACE?.
"""


def find_spec(where):
    """The project's plan file, if it has one."""
    root = Path(where).resolve()
    for candidate in (root / "overnight" / "steps.yaml",
                      root / "tools" / "overnight" / "steps.yaml"):   # older convention
        if candidate.exists():
            return candidate
    return None


def find_runs(where):
    """Every run directory under `<where>/overnight/runs/`, newest log first.

    An EMPTY run.log is not a run. Nothing writes one any more, but a project that
    ran an older version of this runner can carry one - `--list` created it - and
    reporting it as a run IN FLIGHT is worse than ignoring it.
    """
    root = Path(where).resolve()
    candidates = [c for c in sorted(root.glob("overnight/runs/*/run.log"))
                  if c.stat().st_size]
    candidates += [c for c in sorted(root.glob("out/overnight/*/run.log"))
                   if c.stat().st_size]                             # older convention
    return sorted({c.parent for c in candidates},
                  key=lambda d: (d / "run.log").stat().st_mtime, reverse=True)


# NEEDS MERGE is blocking but is NOT in RERUN_OUTCOMES: the work exists on a
# scratch branch, so re-running the step would do it a second time. Only a person
# can say how it lands. OVER BUDGET is in both, like STUCK: a resume will re-run
# it, but not until a person has changed something about the step or the cap.
BLOCKING_OUTCOMES = ("STUCK", "HALTED", "NEEDS MERGE", OVER_BUDGET)


def print_mode(where):
    """Which mode `/overnight` should take, decided from the plan file alone.

    The skill should not have to reason about file states: the project already
    knows the answer, so it is computed here and printed as one word. BLOCKED is
    tested FIRST and is a hard stop - a STUCK step has had three attempts and a
    diagnostic, a HALTED step means somebody else committed to the branch
    mid-step, and neither is a thing the skill can resolve.

    Exits non-zero on BLOCKED so a shell can branch on it too.
    """
    root = Path(where).resolve()
    spec_path = find_spec(root)
    if spec_path is None:
        print("PLAN")
        print(f"reason: no plan file under {root} - there is nothing to run")
        return 0
    try:
        spec = load_spec(spec_path)
    except SpecError as exc:
        print("BLOCKED")
        print(f"reason: the plan at {spec_path} is not valid: {exc}")
        return 3
    steps = spec["steps"]
    blocked = [s for s in steps
               if (s.get("done") or {}).get("outcome") in BLOCKING_OUTCOMES]
    if blocked:
        print("BLOCKED")
        print(f"reason: {len(blocked)} step(s) need a person before this plan can go"
              " further - a stuck step has already had every retry the runner has,"
              " a halted step means somebody else committed to the branch, and an"
              " over-budget step wants a smaller brief or a bigger cap.")
        print(f"spec: {spec_path}")
        for step in blocked:
            done = step["done"]
            print(f"  {step['id']:<30} {done['outcome']:<8} {done.get('at', '')}"
                  f"  {str(done.get('note', ''))[:80]}")
        return 3
    resumable = [s for s in steps if is_resumable(s)]
    if resumable:
        print("RUN")
        print(f"reason: {len(resumable)} of {len(steps)} step(s) still to run")
        print(f"spec: {spec_path}")
        for step in resumable:
            print(f"  {step['id']:<30} {step['kind']:<8} {step.get('title', '')}")
        return 0
    print("REPLACE?")
    print(f"reason: every step in {spec_path} completed - ask before replacing the plan")
    print(f"spec: {spec_path}")
    return 0


def print_progress(where, which=""):
    """A digest of a run in flight, or of the last one to finish.

    Deterministic and cheap, so `/overnight progress` reports facts rather than an
    LLM's reading of a log: what has passed, what is running now and for how long,
    what needs the operator, and the one-line reason to look at each.

    The outcomes come from the PLAN FILE, which is the ledger; the run directory
    supplies only the log and the artefacts.
    """
    root = Path(where).resolve()
    runs = find_runs(root)
    if which:
        runs = [r for r in runs if r.name == which] or runs
    spec_path = find_spec(root)
    if not runs and spec_path is None:
        print(f"no overnight run and no plan found under {root}"
              " (looked in overnight/ and out/overnight/)")
        return 1
    steps = []
    if spec_path is not None:
        try:
            steps = load_spec(spec_path)["steps"]
        except SpecError as exc:
            print(f"the plan at {spec_path} is not valid: {exc}")
    ran = [(s["id"], dict(s["done"], kind=s["kind"])) for s in steps if s.get("done")]
    out = runs[0] if runs else None
    log = read_text(out / "run.log") if out else ""
    lines = log.splitlines()
    # `run finished` and not `run finished:` - the line carries the run's cost
    # total between the words and the colon, and matching the colon made --progress
    # report a finished run as IN FLIGHT.
    finished = "run finished" in log

    print(f"run `{out.name if out else '(not started)'}`  ({out or root})")
    print(f"status: {'FINISHED' if finished else 'IN FLIGHT'}"
          f"  |  {len(ran)} of {len(steps)} step(s) recorded"
          f"  |  self-reported cost "
          f"${sum(float(e.get('cost_usd') or 0) for _, e in ran):.2f}")
    if "RUNNING WITHOUT A GIT UNDO" in log:
        print("        NOTE: this run has no git undo.")
    if spec_path:
        print(f"plan:   {spec_path}   (the ledger: every outcome is recorded in it)")
    print()

    counts = {}
    for _, entry in ran:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    print("  " + ("  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "(nothing yet)"))
    print()
    for sid, entry in ran:
        flag = " <-- NEEDS YOU" if entry["outcome"] in (
            "STUCK", "HALTED", "FAIL", "INCONCLUSIVE", "REVIEW FAIL",
            "REVIEW REWORK FAILED", "REFLECT REVERTED", OVER_BUDGET) else ""
        print(f"  {sid:<30} {entry['outcome']:<22} {entry.get('minutes', 0):>5.0f} min"
              f"  {str(entry.get('note', ''))[:70]}{flag}")
    pending = [s["id"] for s in steps if is_resumable(s)]
    if pending:
        print(f"\n  still to run: {', '.join(pending)}")

    started = [l for l in lines if "] start (" in l]
    # PARKED FIRST, and instead of "in flight". A parked run's last `start (` line
    # is the step it stopped on, which could be hours old - reported as in flight
    # that reads exactly like a hang, which is the one thing parking must never
    # look like.
    parked = [i for i, l in enumerate(lines) if "PARKED after" in l]
    resumed = [i for i, l in enumerate(lines) if "RESUMING:" in l]
    is_parked = bool(parked) and not finished and (not resumed or parked[-1] > resumed[-1])
    if is_parked:
        print(f"\n  PARKED - waiting for the account, not stuck: {lines[parked[-1]]}")
        probes = [l for l in lines
                  if "still walled" in l or ": ALIVE" in l]
        if probes:
            print(f"    last probe: {probes[-1].strip()}")
    elif started and not finished:
        print(f"\n  in flight: {started[-1]}")
    if out:
        for name, why in (("discarded-commits.md", "commits a reset would have thrown away"),
                          ("remediation.md", "a diagnostic's plan for the third attempt"),
                          ("verdict.json", "a review's typed verdict")):
            hits = sorted(out.glob(f"*/{name}"))
            if hits:
                print(f"\n  {len(hits)} x {name} ({why}):")
                for hit in hits[:10]:
                    print(f"    {hit.relative_to(out)}")
        print(f"\n  tail: {out / 'run.log'}")
        for line in lines[-6:]:
            print("    " + line)
    return 0
def main():
    # FIRST, before anything can print: a scheduled run's console is cp1252 and
    # every line the runner echoes may carry worker-written text.
    forgiving_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", default="", help="path to the steps YAML")
    parser.add_argument("--progress", nargs="?", const=".", default=None,
                        metavar="PROJECT",
                        help="digest a run in flight (or the last one) and exit;"
                             " defaults to the current directory")
    parser.add_argument("--run", default="", help="with --progress: name the run directory")
    parser.add_argument("--mode", nargs="?", const=".", default=None, metavar="PROJECT",
                        help="print which mode /overnight should take - BLOCKED, PLAN,"
                             " RUN or REPLACE? - and exit; defaults to the current"
                             " directory. Exits 3 on BLOCKED.")
    parser.add_argument("--repo", default="", help="repository root (default: found above the spec)")
    parser.add_argument("--until", default="", metavar="TIME",
                        help="stop STARTING steps at this time: `07:30` for the"
                             " next 07:30 (tomorrow's, if today's has passed), or"
                             " `2026-09-08 07:30` for one exact moment. A step"
                             " already running is never interrupted. Wins over"
                             " --hours and over the spec")
    parser.add_argument("--hours", type=float, default=None,
                        help="stop STARTING steps after this many hours."
                             " Superseded by --until, which says what you"
                             " actually mean and does not decay while you type"
                             " the command; kept for old plans and scripts")
    parser.add_argument("--on-wall", default="", choices=["", "park", "stop"],
                        help="what to do when the workers stop answering entirely"
                             " (a usage limit, a logged-out CLI, no network):"
                             " `park` (the default) waits and probes until the"
                             " account is back, `stop` ends the run and leaves the"
                             " remaining steps pending")
    parser.add_argument("--wall-threshold", type=int, default=0,
                        help="consecutive workers returning nothing before the wall"
                             " is called (default 3)")
    parser.add_argument("--park-poll-min", type=float, default=0,
                        help="minutes between probes while parked (default 30)")
    parser.add_argument("--stall-min", type=float, default=None,
                        help="kill a worker whose log has not grown for this many"
                             " minutes (default 10, 0 to disable)")
    parser.add_argument("--continuations", type=int, default=None,
                        help="extra workers one attempt may use when the budget cuts"
                             " one off part-way, carrying its work forward"
                             " (default 0, off)")
    parser.add_argument("--from", dest="from_step", default="")
    parser.add_argument("--only", default="", help="comma-separated step ids")
    parser.add_argument("--rerun", action="store_true", help="re-run steps already PASSed in state")
    parser.add_argument("--reset-state", action="store_true", help="forget prior outcomes")
    parser.add_argument("--dry-run", action="store_true", help="no workers; gates only")
    parser.add_argument("--fake-worker", default="", help="python script standing in for claude")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--print-brief", default="")
    parser.add_argument("--format", action="store_true", help="print the spec format and exit")
    args = parser.parse_args()

    if args.format:
        print(SPEC_FORMAT)
        return 0
    if args.mode is not None:
        return print_mode(args.mode)
    if args.progress is not None:
        return print_progress(args.progress, args.run)
    if not args.spec:
        parser.error("--spec is required (or use --mode / --progress / --format)")
    try:
        runner = Runner(args)
    except SpecError as exc:
        # A bad `until`, isolation or on_wall used to leave a traceback, which is
        # the least readable thing the runner can print and the most likely to be
        # read at midnight by somebody about to walk away from it.
        raise SystemExit(f"the run cannot start: {exc}")
    if args.reset_state:
        # Exclusive, and first: forgetting the outcomes is a decision of its own,
        # and the launch that follows is the user's separate command.
        runner.reset_ledger()
        print("--reset-state: outcomes forgotten. Nothing was launched;"
              " re-run without --reset-state to start the plan from the top.")
        return 0
    if args.list:
        for s in runner.spec["steps"]:
            done = s.get("done") or {}
            flag = "" if not is_resumable(s) else ("  <- to run" if done else "")
            print(f"{s['id']:<28} {s['kind']:<8} {done.get('outcome', '-'):<14}"
                  f" {s.get('title', '')}{flag}")
        return 0
    if args.print_brief:
        step = next((s for s in runner.spec["steps"] if s["id"] == args.print_brief), None)
        if not step:
            raise SystemExit(f"unknown step: {args.print_brief}")
        if step["kind"] == "build":
            print(runner.build_brief(step, 1))
        elif step["kind"] == "reflect":
            ran = set(runner.done_map())
            print(runner.reflect_brief(step, [s for s in runner.spec["steps"]
                                              if s["id"] not in ran]))
        else:
            print("(review briefs are composed from the reviewed commit at run time)")
        return 0
    try:
        return runner.main()
    finally:
        # Here rather than inside Runner.main, so a SystemExit out of preflight -
        # a dirty tree, a universal gate that fails - still gives the lock back.
        runner.release_lock()


if __name__ == "__main__":
    sys.exit(main())
