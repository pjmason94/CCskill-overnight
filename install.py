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
"""install.py - make this checkout the `overnight` skill, in one step.

    python install.py                 # user scope:    ~/.claude/skills/overnight
    python install.py --project .     # project scope: <dir>/.claude/skills/overnight
    python install.py --force         # replace whatever is there already
    python install.py --copy          # copy instead of linking (last resort)
    python install.py --uninstall     # remove the installed skill
    python install.py --check         # report what is installed, change nothing

Claude Code looks for skills in `<scope>/.claude/skills/<name>/SKILL.md` and that
path is not configurable. So the checkout is the source of truth and the skill
directory is a LINK to it - a directory junction on Windows (no administrator
rights needed), a symlink elsewhere. Real files stay under version control here;
the harness keeps reading the path it expects; an edit in either place is the
same edit.

`--copy` exists for the case where linking is impossible (a filesystem that
cannot do it). It is worse in the way that matters: the copy drifts silently from
the checkout, and a fix made here never reaches the skill that is actually
loaded. Prefer the link. Re-running install.py refreshes a copy.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
SKILL_NAME = "overnight"
REQUIRED = ("SKILL.md", "overnight.py", "selftest.py", "fake_worker.py")


def target_dir(project):
    """Where Claude Code will look for the skill."""
    base = Path(project).resolve() if project else Path.home()
    return base / ".claude" / "skills" / SKILL_NAME


def link_target(path):
    """The path a link points at, or None if `path` is not a link.

    On Windows a junction is not a symlink and `Path.readlink` raises on older
    Pythons, so resolve() is the portable question: a junction resolves through.
    """
    if not path.exists():
        return None
    try:
        if path.is_symlink():
            return Path(os.readlink(path)).resolve()
    except OSError:
        pass
    resolved = path.resolve()
    return resolved if resolved != path else None


def describe(path):
    if not path.exists():
        return "not installed"
    pointed = link_target(path)
    if pointed and pointed == SOURCE:
        return f"linked to this checkout ({SOURCE})"
    if pointed:
        return f"LINKED ELSEWHERE -> {pointed}"
    return "a real directory (a copy, or another install) - it will drift"


def make_link(source, target):
    """Junction on Windows, symlink elsewhere. Returns an error string or ''."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                              capture_output=True, text=True)
        return "" if done.returncode == 0 else (done.stdout + done.stderr).strip()
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError as exc:
        return str(exc)
    return ""


def copy_tree(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "runs", ".pytest_cache"))


def remove(path):
    """Delete a link WITHOUT following it into the checkout."""
    if path.is_symlink() or (os.name == "nt" and link_target(path)):
        try:
            path.rmdir()            # unlinks a junction; never touches the target
            return
        except OSError:
            os.unlink(path)
            return
    shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="",
                        help="install into <dir>/.claude/skills instead of the user profile")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing skill directory")
    parser.add_argument("--copy", action="store_true",
                        help="copy the files instead of linking (drifts; last resort)")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--check", action="store_true", help="report and exit")
    args = parser.parse_args()

    missing = [f for f in REQUIRED if not (SOURCE / f).exists()]
    if missing:
        sys.exit(f"this does not look like the overnight checkout: missing {missing}\n"
                 f"  looked in {SOURCE}")

    target = target_dir(args.project)
    print(f"source: {SOURCE}")
    print(f"target: {target}")
    print(f"status: {describe(target)}")

    if args.check:
        return 0

    if args.uninstall:
        if not target.exists():
            print("nothing to uninstall")
            return 0
        remove(target)
        print("uninstalled. The checkout is untouched.")
        return 0

    if target.exists():
        pointed = link_target(target)
        if pointed == SOURCE and not args.copy:
            print("already installed and pointing here - nothing to do.")
            return 0
        if not args.force:
            sys.exit("refusing to replace what is already there. Re-run with --force"
                     " if that is what you want (the checkout is never touched).")
        remove(target)
        print("removed the previous install")

    if args.copy:
        copy_tree(SOURCE, target)
        print("COPIED. This copy will drift from the checkout - re-run install.py"
              " after every change here, or install with a link instead.")
    else:
        error = make_link(SOURCE, target)
        if error:
            sys.exit(f"could not create the link: {error}\n"
                     "If this filesystem cannot link, re-run with --copy and accept"
                     " that the copy must be refreshed after every change.")
        print("linked.")

    for name in REQUIRED:
        if not (target / name).exists():
            sys.exit(f"install verification FAILED: {target / name} is not readable")
    print("verified: SKILL.md and the runner are readable through the install path.")
    print("\nNext:")
    print("  1. python selftest.py            (must print SELFTEST PASS)")
    print("  2. start a new Claude Code session so the skill list is re-read")
    print("  3. /overnight plan               (in the project you want a run for)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
