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
"""tally.py - where a session's tokens went, from its stream-json log.

    python tools/tally.py <log, directory or interactive transcript .jsonl> [...]

Reads a worker log the runner wrote (attempt-*.log, rework.log, review.log,
reflect.log, diagnostic.log) or an interactive Claude Code transcript
(~/.claude/projects/<slug>/<session>.jsonl): both carry one `assistant` record
per streamed content block with the API call's `usage` on each, so a call is
identified by its message id and counted once. A worker log also ends in a
`result` event whose totals are authoritative and are used when present.

Prints per session: model, API calls, cost buckets (uncached input, cache
writes, cache reads, output) at list price, the brief size, the peak and mean
context per call, and tool calls with the bytes each returned. Then totals, so
the biggest bucket is the one to attack. The cost is arithmetic, not a bill:
list price, 1h cache writes at 2x input, reads at 0.1x - which reconciles to the
`total_cost_usd` a worker reports.
"""
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

# USD per million tokens: (input, cache write at the 1h TTL, cache read, output).
PRICES = {
    "opus": (5.0, 10.0, 0.5, 25.0),
    "sonnet": (2.0, 4.0, 0.2, 10.0),
    "haiku": (1.0, 2.0, 0.1, 5.0),
}
READ_CMDS = re.compile(r"^\s*(cat|sed|head|tail|grep|rg|find|ls|wc|type|Get-Content|"
                       r"Select-String|awk|cut|tree|dir|git show|git diff|git log|git status)\b")
LOG_NAMES = re.compile(r"^(attempt-\d+|rework|review|reflect|diagnostic)\.log$")
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")


def price_of(model):
    for key, row in PRICES.items():
        if key in (model or ""):
            return row
    return PRICES["opus"]


def cost_of(usage, model):
    p = price_of(model)
    return {"input": usage["input_tokens"] * p[0] / 1e6,
            "cache_write": usage["cache_creation_input_tokens"] * p[1] / 1e6,
            "cache_read": usage["cache_read_input_tokens"] * p[2] / 1e6,
            "output": usage["output_tokens"] * p[3] / 1e6}


def tally(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    brief = 0
    if "=== BRIEF ===" in text and "=== WORKER OUTPUT ===" in text:
        brief = len(text.split("=== BRIEF ===", 1)[1].split("=== WORKER OUTPUT ===", 1)[0])
    calls = {}                       # message id -> usage of that API call
    order = []
    model = ""
    tools = Counter()
    result_bytes = Counter()
    id_to_tool = {}
    bash_kind = {}
    largest = []                     # (bytes, tool, hint)
    hints = {}
    reported = None
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant":
            u = message.get("usage") or {}
            mid = message.get("id")
            if u and mid:
                model = model or message.get("model", "")
                row = {k: int(u.get(k) or 0) for k in USAGE_KEYS}
                if mid not in calls:
                    order.append(mid)
                    calls[mid] = row
                else:
                    # Output accumulates across the blocks of one call; the
                    # context figures are the same on every one of them.
                    calls[mid]["output_tokens"] = max(calls[mid]["output_tokens"],
                                                      row["output_tokens"])
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tools[name] += 1
                    id_to_tool[block.get("id")] = name
                    inp = block.get("input") or {}
                    hint = str(inp.get("command") or inp.get("file_path")
                               or inp.get("pattern") or inp.get("prompt") or "")[:70]
                    hints[block.get("id")] = hint
                    if name in ("Bash", "PowerShell"):
                        is_read = bool(READ_CMDS.match(hint)) or " | grep" in hint
                        bash_kind[block.get("id")] = "read" if is_read else "run"
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                size = len(str(body or ""))
                tid = block.get("tool_use_id")
                name = id_to_tool.get(tid, "?")
                if name in ("Bash", "PowerShell"):
                    name += "/" + bash_kind.get(tid, "run")
                result_bytes[name] += size
                largest.append((size, name, hints.get(tid, "")))
        elif kind == "result":
            reported = event
    usage = Counter()
    for mid in order:
        usage.update(calls[mid])
    if reported and isinstance(reported.get("usage"), dict):
        usage = Counter({k: int(reported["usage"].get(k) or 0) for k in USAGE_KEYS})
    contexts = [calls[m]["input_tokens"] + calls[m]["cache_creation_input_tokens"]
                + calls[m]["cache_read_input_tokens"] for m in order]
    first = calls[order[0]] if order else {}
    largest.sort(reverse=True)
    return {"path": path, "model": model, "calls": len(order), "brief": brief,
            "peak": max(contexts, default=0),
            "mean": (sum(contexts) / len(contexts)) if contexts else 0,
            "usage": usage, "first": first, "tools": tools, "bytes": result_bytes,
            "largest": largest[:5], "cost": cost_of(usage, model),
            "reported": (reported or {}).get("total_cost_usd"),
            "thinking": ((reported or {}).get("usage") or {}).get(
                "output_tokens_details", {}).get("thinking_tokens")}


def find_logs(args):
    for arg in args:
        path = Path(arg)
        if path.is_file():
            yield path
        else:
            for candidate in sorted(path.rglob("*.log")):
                if LOG_NAMES.match(candidate.name) and "dry-run" not in candidate.parts:
                    yield candidate


def main():
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    rows = [tally(p) for p in find_logs(sys.argv[1:] or ["."])]
    rows = [r for r in rows if r["calls"]]
    if not rows:
        sys.exit("no logs with usage found")
    out.write("session | model | calls | brief KB | results KB | ctx peak/mean | cache write | "
              "cache read | out (thinking) | first call write/read | "
              "$cw $cr $out = $list (reported)\n")
    total = Counter()
    for r in rows:
        u, c, f = r["usage"], r["cost"], r["first"]
        label = f"{r['path'].parent.name}/{r['path'].stem}"[-60:]
        think = f" ({r['thinking'] / 1000:.0f}k)" if r["thinking"] else ""
        rep = f" ({r['reported']:.2f})" if r["reported"] else ""
        out.write(f"{label} | {r['model']} | {r['calls']} | {r['brief'] / 1024:.0f} | "
                  f"{sum(r['bytes'].values()) / 1024:.0f} | "
                  f"{r['peak'] / 1000:.0f}k/{r['mean'] / 1000:.0f}k | "
                  f"{u['cache_creation_input_tokens'] / 1000:.0f}k | "
                  f"{u['cache_read_input_tokens'] / 1000:.0f}k | "
                  f"{u['output_tokens'] / 1000:.0f}k{think} | "
                  f"{f.get('cache_creation_input_tokens', 0) / 1000:.0f}k/"
                  f"{f.get('cache_read_input_tokens', 0) / 1000:.0f}k | "
                  f"{c['cache_write']:.2f} {c['cache_read']:.2f} {c['output']:.2f}"
                  f" = {sum(c.values()):.2f}{rep}\n")
        total.update(c)
    grand = sum(total.values())
    out.write(f"\nTOTAL over {len(rows)} sessions at list price: ${grand:.2f}\n")
    for key in ("cache_read", "cache_write", "output", "input"):
        out.write(f"  {key:12s} ${total[key]:7.2f}  {100 * total[key] / grand:4.0f}%\n")

    out.write("\n--- tool calls and the bytes their results put into context ---\n")
    calls, size = Counter(), Counter()
    for r in rows:
        calls.update(r["tools"])
        size.update(r["bytes"])
    for name, n in size.most_common():
        out.write(f"  {name:18s} {n / 1024:8.0f} KB returned\n")
    out.write("  calls: " + ", ".join(f"{k}={v}" for k, v in calls.most_common()) + "\n")

    out.write("\n--- per session: tool calls, and the five largest results ---\n")
    for r in rows:
        label = f"{r['path'].parent.name}/{r['path'].stem}"[-60:]
        top = ", ".join(f"{k}={v}" for k, v in r["tools"].most_common(8))
        out.write(f"  {label}: {top}\n")
        for size_, name, hint in r["largest"]:
            out.write(f"      {size_ / 1024:6.0f} KB  {name:14s} {hint}\n")
    out.flush()


if __name__ == "__main__":
    main()
