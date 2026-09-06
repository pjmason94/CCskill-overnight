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
                  "REVERTED BY REVIEW", "HALTED"}
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
    " command through a filter instead of dumping its output.\n")

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
            print(line, flush=True)

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


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
    for key in ("outcome", "at", "sha", "attempts", "minutes", "cost_usd", "tier",
                "note", "reworked", "of", "findings", "added", "removed"):
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
        hours = args.hours if args.hours is not None else float(
            self.run_cfg.get("hours", 6))
        self.stop_at = time.time() + hours * 3600
        self.hours = hours
        self._ran_this_session = set()

    # -- the ledger ----------------------------------------------------------
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
                    "reworked", "of", "findings", "added", "removed"):
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

    def add_worktree(self, step_id, base, detach=False):
        """A private tree on a scratch branch, based at `base`."""
        path = self.worktree_root() / (self.ref_safe(step_id) if not detach else "_probe")
        if path.exists():
            self.remove_worktree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add", "--detach", str(path), base] if detach else \
            ["worktree", "add", "-B", self.scratch_branch(step_id), str(path), base]
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

    def prune_worktrees(self):
        """After a crash: git forgets the registration, the directory remains."""
        self.git("worktree", "prune", tree=self.repo)
        root = self.worktree_root()
        if not root.exists():
            return
        known = self.git("worktree", "list", "--porcelain", tree=self.repo)[1]
        for path in sorted(root.iterdir()):
            if path.is_dir() and str(path) not in known:
                self.log(f"    removing an orphaned worktree from an earlier run: {path}")
                shutil.rmtree(path, ignore_errors=True)

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

    def worker_argv(self, kind, tier, schema=None, disallow=()):
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
                if now >= next_beat:
                    handle.flush()
                    size = log_path.stat().st_size if log_path.exists() else 0
                    beats += 1
                    # Every beat to run.log, every tenth to stdout. A watcher gets
                    # a line every ten minutes instead of every one, and the file
                    # loses nothing.
                    self.log(f"{tag} still running, {(now - started) / 60:.0f} min,"
                             f" log {size / 1024:.0f} KB", echo=(beats % 10 == 0))
                    next_beat = now + 60
                time.sleep(2)
            elapsed = time.time() - started
            handle.write(f"\n\n=== exit {code} after {elapsed / 60:.1f} min ===\n")
        result = self.result_of(log_path)
        cost = result.get("total_cost_usd") or 0.0
        # Held per step until the step is recorded; the run's total is then the sum
        # of the ledger and nothing accumulates it separately.
        self.step_cost[step_id] = self.step_cost.get(step_id, 0.0) + cost
        return code, elapsed, result

    @staticmethod
    def result_of(log_path):
        """The final `result` event of a stream-json log, or {}."""
        text = read_text(log_path)
        for line in reversed(text.splitlines()):
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "result":
                return payload
        return {}

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
        started = time.time()
        remediation = ""
        note = ""
        for attempt in range(1, attempts + 1):
            # Re-read the baseline PER ATTEMPT, not per step. A commit that landed
            # between attempts is then below the baseline and cannot be reset away.
            # This narrows the window; safe_reset closes what is left of it.
            attempt_base = base_sha or self.head()
            if attempt == 3 and (step_dir / "remediation.md").exists():
                remediation = read_text(step_dir / "remediation.md")
                self.log(f"[{step['id']}] attempt 3 ingests the remediation plan")
            brief = self.build_brief(step, attempt, remediation, rework)
            suffix = "rework" if rework else f"attempt-{attempt}"
            log_path = step_dir / f"{suffix}.log"
            if self.args.dry_run:
                # Said as a stub, not as a worker: "worker exit 0 after 0.0 min"
                # reads like a worker that ran and found nothing to do.
                self.log(f"[{step['id']}] {suffix}: (dry run) brief composed for"
                         f" {tier['model']}/{tier['effort']}, {len(brief)} chars ->"
                         f" {log_path.name}. No worker is spawned; the gates run next.")
                log_path.write_text(f"(dry run)\n\n=== BRIEF ===\n{brief}\n", encoding="utf-8")
                code, elapsed, result = 0, 0.0, {}
            else:
                self.log(f"[{step['id']}] {suffix}: worker starting ({tier['model']}/"
                         f"{tier['effort']}, {len(brief)} chars) -> {log_path.name}")
                code, elapsed, result = self.run_worker(
                    self.worker_argv("build", tier), brief, log_path, timeout,
                    f"[{step['id']}] {suffix}:", step["id"], "build")
            if not self.args.dry_run:
                self.log(f"[{step['id']}] {suffix}: worker exit {code} after"
                         f" {elapsed / 60:.1f} min"
                         + (" | " + self.one_line(result) if result else ""))
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write("\n=== GATES ===\n")
                ok, failed, output = self.run_gates(self.all_gates(step), handle)
            if ok:
                sha = self.head()
                moved = sha != before
                note = f"committed at {sha[:8]}" if moved else "gates pass but HEAD did not move"
                self.log(f"[{step['id']}] PASS ({note})")
                return {"outcome": "PASS", "attempts": attempt, "sha": sha,
                        "minutes": round((time.time() - started) / 60, 1),
                        "note": note, "summary": (result.get("result") or "")[:2000]}
            note = f"failed the gate: {failed}"
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
                 f" stop starting after {self.hours} h, dry_run={self.args.dry_run},"
                 f" fake_worker={bool(self.args.fake_worker)}")
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
                self.log(f"STOP: {reason}. {len(todo)} step(s) not started:"
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
  hours: 6                       # stop STARTING steps after this many hours
  attempts: 3                    # build attempts before STUCK
  worker_timeout_min: 90
  budget_usd_per_step: 40        # optional, --max-budget-usd on every worker
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
INCONCLUSIVE, SKIPPED, REWORK FAILED and REVERTED BY REVIEW; anything else is
re-run on resume. `--reset-state` strips every `done:` and commits that.
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
# can say how it lands.
BLOCKING_OUTCOMES = ("STUCK", "HALTED", "NEEDS MERGE")


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
              " and a halted step means somebody else committed to the branch.")
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
            "REVIEW REWORK FAILED", "REFLECT REVERTED") else ""
        print(f"  {sid:<30} {entry['outcome']:<22} {entry.get('minutes', 0):>5.0f} min"
              f"  {str(entry.get('note', ''))[:70]}{flag}")
    pending = [s["id"] for s in steps if is_resumable(s)]
    if pending:
        print(f"\n  still to run: {', '.join(pending)}")

    started = [l for l in lines if "] start (" in l]
    if started and not finished:
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
    parser.add_argument("--hours", type=float, default=None)
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
    runner = Runner(args)
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
