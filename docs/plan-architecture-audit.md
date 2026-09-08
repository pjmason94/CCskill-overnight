# Plan - audit the implementation against the theory

> **Executed 2026-09-08.** The findings, F and G, and the proposed fix set are in
> `audit-findings.md`. The heartbeat task below is done. This file is kept as the
> record of what was asked.

Written 2026-09-08 for a fresh **FH** session. Self-contained: it assumes no
memory of the conversation that produced it, and nothing in it needs a transcript
to be recovered.

## FIRST: check the state of the tree

The session that wrote this file finished a body of `1.0.3` work and was waiting
on a full self-test to commit it. **Run `git status` before anything else.** It
resolves to one of three states.

**Clean, with a recent `1.0.3` commit** - the expected case. The suite passed and
the work landed. Nothing to do; go to the audit.

**Dirty** - the session ended before the suite returned. The work is finished and
reviewed, not a half-thought. Land it:

    python selftest.py          # must print SELFTEST PASS
    git add -A
    git commit                  # commit only - DO NOT push

Suggested subject, in this project's style:
`the builder is sonnet and the money goes to the judgement`

**Dirty, and the suite FAILS** - this is the one case that is genuinely yours to
solve, and it was handed over deliberately rather than fixed in a context that
was nearly full. Fix the failure before the audit, because the audit's own
findings will need a green suite to land against. Note that the change most
likely to have broken it is the addition to `TOOL_USAGE_NOTE` in `overnight.py`
(a paragraph asking every worker to re-read its own diff before committing) -
brief composition and its sizes are asserted in several sections, and a changed
note changes those. If a check pins a byte count or a substring of the note, the
check is what needs updating, not the note - but verify that rather than assuming
it, because the note is also passed to every worker kind and a genuine regression
would look similar.

### What the uncommitted work is

`DEFAULT_TIERS` build moved to `sonnet/medium`, with review, reflect and
diagnostic held at `opus/high` **deliberately** - the reviewer is the
compensating control for a cheaper builder, so cutting both at once removes what
made the first cut safe. The planner is told to cut steps to fit sonnet and to
justify any escalation in one sentence. `references/planning.md` no longer
mandates the whole suite as a per-step gate. Briefs must now name what a step
**feeds** as well as what feeds it. Every worker is asked to re-read its own diff
before committing. Two new sections in `docs/token-efficiency.md`, and roadmap
item 14 (an `integrate` step, and `git bisect` as the deterministic answer to
"which step broke the suite").

The open `[1.0.3]` changelog section is the authority if this summary and the
diff disagree.

## The thesis

This project documents its own architecture unusually well - `README.md`,
`SKILL.md`, `references/`, `docs/token-efficiency.md`, `docs/ROADMAP.md` and two
`CLAUDE.md` files all make normative claims about how the runner behaves and how
a plan should be cut.

**The audit is not of the theory. It is of whether the code and the procedures
actually do what those documents say.** Where they diverge, the document is
usually not the thing that is wrong - it is the thing nobody re-read.

Re-litigating settled design is explicitly out of scope. Finding places where the
implementation quietly does something else is the whole job.

## Why this is worth a night: the defect class is confirmed, not hypothetical

On 2026-09-08 three instances were found by accident, in under an hour, while
looking for something else:

1. **`references/planning.md` instructed the planner to append the whole test
   suite to every build step** (`run.gates`), while `README.md` argued for narrow
   per-step gates. A 10-20 minute suite after each of twenty 15-minute steps is
   three to seven hours of a night. The cost was *mandated by the procedure*, not
   merely undocumented.
2. **Briefs named what feeds a step and never what a step feeds.** The downstream
   half existed nowhere in the repository except in two `overnight.py` comments
   lamenting its absence. A real run had a step reach back and edit the module
   upstream of it so its own work would fit.
3. **`DEFAULT_TIERS` built at `opus/medium`** while `README.md` said mechanical
   work is sonnet, and measurement said sonnet costs $0.051 an API call against
   opus at $0.090 with no reliability penalty visible in the ledger.

All three are fixed in the open `1.0.3` section. **Do not re-find them.** They
are listed here as calibration: this is the shape of what you are hunting, and
three in one hour by accident implies more under a deliberate search.

## Hard constraints - read before touching anything

1. **This checkout IS the live skill.** `install.py` links it to
   `~/.claude/skills/overnight/`, so every project on this machine loads these
   files and an edit here is live for the next session that reads them. A run
   already in flight is safe (Python loads `overnight.py` once at process start);
   a run *launched or relaunched* afterwards is not, and neither is a new
   interactive session, which reads `SKILL.md` and `references/` at start. **Do
   not commit while a run could be launched from this checkout.**
2. **`v1.0.2` is tagged, pushed and locked.** Its `CHANGELOG.md` section is never
   edited again. Work folds into the open `[1.0.3] - unreleased` section.
3. **COMMIT, BUT DO NOT PUSH** (Paul, 2026-09-08). `1.0.3` is deliberately kept
   open and local: the audit's own findings are expected to land in it, and a
   version is only cut once its release notes are written and its tag pushed.
   Commit freely and often - pushing is what waits.
3. **The full self-test must pass before any commit or push** - `python
   selftest.py`, 12-30 minutes, 292 checks, prints `SELFTEST PASS`. While
   working, `--only <n>` / `--from <n>` run part of it and print `SELFTEST
   PARTIAL OK`. Every section declares its check count and the suite fails if a
   section makes a different number, so adding a check means updating `SECTIONS`.
4. `CHANGELOG.md` and `README.md` are updated **in the same commit** as the
   change, never afterwards.
5. ASCII only. No heredocs, and no interpreter reading stdin - write the file,
   run the file. Paul's shell is **PowerShell**; the Bash tool is Git Bash, so
   translate before handing him a command.
6. Nothing project-specific in the runner, the skill or the references. Nothing
   confidential - this repository is public.

## One concrete task, carried over rather than audited

**`selftest.py` must emit a percentage-complete heartbeat**, and it is the reason
the rule now exists in the user-level `CLAUDE.md` (2026-09-08). The suite takes 7
to 30 minutes and prints nothing until it is finished, so "how long" cannot be
answered except with a range wide enough to be useless - which happened twice in
one session.

The shape, per that rule:

    [ 47%]  11/23 sections | 8.2 min elapsed | ~9.3 min left

Requirements, each of which is the rule rather than a preference:

- **The denominator is known before the run starts.** `SECTIONS` already carries
  every section and its declared check count, so both a section-level and a
  check-level percentage are available without discovering anything.
- **The ETA extrapolates from mean unit time so far**, not from a hardcoded
  figure - the spread on this machine is load, not work, so a fixed estimate is
  wrong on exactly the runs where it matters.
- **It goes to a log file, never into an agent's conversation.** A progress line
  streamed into context is re-read on every later API call, which is the most
  expensive place it could possibly live. Run it as
  `python -u selftest.py > run.log 2>&1` and read the tail on demand.
- **Never pipe a long run through `tail`, `grep` or `sort`.** The pipe buffers
  every byte until the process exits, so a perfectly streamed heartbeat produces
  a zero-byte log. This is how the incident happened.
- **Full suite only.** A selective run (`--only 13,17`, `--from 17`) emits no
  heartbeat: those are short, the line is noise in them, and suppressing it keeps
  a partial run's output byte-identical so two can be diffed. A `--quiet` flag
  should also suppress it outright, so CI and the launch gate stay byte-identical
  to what they print today.
- **Whenever a full suite is launched, print the read-back command into the chat**
  with the real absolute log path already substituted, so Paul can watch it in his
  own PowerShell without asking:

      Get-Content "<abs path>\run.log" -Tail 5          # or -Wait to follow

  He must never have to ask how far along a run is, or reconstruct the path.

This is a small change and it is not part of the audit - do it first, or last, but
do not let it become a finding.

## Out of scope

- The five decisions taken 2026-09-07 and recorded in `docs/plan-v1.0.2.md`.
- Roadmap item 13 (the retry ladder) and the step-dependency rework: deferred
  deliberately, and waiting on evidence named in the roadmap.
- Anything in the `[1.0.2]` changelog section. It is locked.
- Rewriting the theory because you disagree with it. If you believe a documented
  principle is wrong, record it as a finding with the evidence and move on - do
  not act on it in the same pass as the audit.

## Facts from the measurement session, so you need not re-derive them

| | |
|---|---|
| overnight Opus workers, FinKit | 856 calls, $77.43, **$0.090/call**, ~110k mean context |
| one long interactive Opus session | 395 calls, $71.77, **$0.182/call**, **290k** mean context |
| sonnet workers, same run | 780 calls, $39.87, **$0.051/call**, 81-170k context |
| cold starts across a run | **under 3%** ($1.30 of $46.52) |
| context re-read share of a worker's bill | **56%** (79% for the long session) |
| the long session's night | 8.38 h span, **5.81 h (69%) idle** - it ran out of plan, not time |
| Woodwork Guru usage wall | **16 steps STUCK in 10 minutes**, all $0.00 |
| reflect during that cascade | `reflect-3` fired mid-cascade and returned NO CHANGE at **$0.00** - barren itself |

Two consequences worth carrying: **cost is calls x context**, and **an in-band
LLM judgement step shares a failure mode with the outage it is meant to detect.**

## The audit axes

Seven, and they are not equal. **A to E are mechanical and delegable. F and G are
the reason this session is FH.**

### A. Doc-to-code contradiction
Every normative claim in `README.md`, `SKILL.md` and `references/*.md` checked
against what `overnight.py` actually does. The planning.md gate bug is this
class. Highest expected yield.

### B. Doc-to-doc contradiction
The same claim stated in two places with different content. Tier guidance
appeared in three places and disagreed. Check `README.md` against `SKILL.md`
against `references/` against both `CLAUDE.md` files.

### C. Claims with no enforcement
Things the documents say **must** happen that nothing checks. "One deliverable
per step", "the brief names the exact test node id", "point at documents, do not
restate them" are all advisory today. For each: could it be validated at spec
load, as `expected_min` now is? Some should stay advisory - say which and why.

### D. Self-test coverage against claimed behaviour
The project rule is "every change is covered by the self-test, or says why not".
Test that claim. **The clock had no test at all until v1.0.2** despite being the
one path that ends a run with steps pending and no failure to show. What else is
documented, load-bearing and untested? Rank by blast radius, not by ease.

### E. Vestigial and unreachable
Code paths implemented and never exercised; spec keys documented and never read;
flags nothing tests. `--copy`, `fresh_shell`, `cmd_empty`, `kind: gate`,
`file:` gates, `--reset-state` are candidates - verify rather than assume.

### F. The efficiency objective is now mis-stated - FH
`CLAUDE.md` sets the bar as "an unattended run must not be significantly more
token-hungry than doing the same work in an interactive session... 2x is not
acceptable." The measurement above says the runner **beats** a long interactive
session by roughly 2x per call. So the stated objective is defending against a
risk that did not materialise, while the real risk - a night that produces little
because steps were mis-cut, or a run that marches into a wall - is unmeasured by
it. **Propose what the objective should be instead**, in terms the runner can
actually report on. This is a judgement call and it is the most valuable single
output of the audit.

One nuance to carry in, checked against Anthropic's published guidance on
2026-09-08. They publish roughly the opposite claim - that a long continuous
session often costs *less* than splitting the same work into several short ones,
because each split pays a cold start. Our measurement says cold starts are under
3% of a run while context growth is the dominant term, so on this machine's data
that reasoning does not carry. **Both are probably true at different points on
one curve**: `docs/token-efficiency.md` already found a U, dear at 11 calls
because a 55k prefix is amortised over eleven of them and dear at 100 because
context grew to 119k, with a floor around 20-55 calls. Their claim describes the
left arm, ours the right. Any restatement of the objective should be expressed in
terms of that curve rather than as a flat preference for short or long, and
should note that the project's 7-15 minute step already sits near its floor.

### G. Where judgement lives, and what it shares a failure mode with - FH
The reflect-goes-barren finding generalises. For every place the runner delegates
a decision to a model - review verdicts, reflect rewrites, diagnostics,
continuation handovers - ask: *what happens to this decision when the thing it is
deciding about is what has failed?* Then ask whether a deterministic check should
gate it. The usage-wall circuit breaker is the worked example: counting empty
returns in Python cannot fail the way the thing it watches fails.

Anthropic's published guidance is relevant and was checked on 2026-09-08:
multi-agent systems are documented at **3-10x the tokens** of a single agent,
with the advice to start with single agents and "decompose work by context
boundaries, not problem types". So the answer to a gap here is rarely "add
another model in the loop".

## The delegation contract - non-negotiable

FH orchestrates. Sonnet subagents do A through E. **The audit's own token cost is
part of what is being audited, so the I/O discipline is strict.**

**Each subagent gets:** one axis, an explicit file list (never "read the repo"),
and this output contract. It may read `overnight.py` in ranges; it may not paste
it back.

**Each subagent returns ONLY lines in this form, and nothing else** - no
preamble, no closing summary, no restatement of the task:

    <AXIS letter> | <1-3> | <file:line> | <claim, max 15 words> | <contradicted by file:line, or NONE> | <fix, max 15 words>

ending with a single line:

    COUNT: <n>

Severity: **1** = a live run behaves wrongly or wastes real time; **2** = a
procedure misleads the planner or a user; **3** = cosmetic or stale wording.
**Cap each subagent at 25 finding lines.** If it has more, it returns the 25
highest-severity and appends `TRUNCATED: <total>`.

FH does not paste subagent output into its own reasoning wholesale - it collates
into `docs/audit-findings.md` and works from that file.

Run the A-E subagents as **one parallel batch**, not serially. Expect five to
eight of them; do not exceed ten.

## Sequence

1. Read this file, `CLAUDE.md`, and the open `[1.0.3]` changelog section. Nothing
   else yet.
2. Launch the A-E batch with explicit file lists.
3. Collate into `docs/audit-findings.md`, ranked by severity then blast radius.
4. **Verify every severity-1 finding yourself before acting on it.** A subagent
   working from a file list can misread scope, and a false severity-1 that
   reaches a fix is worse than a missed one.
5. Do F and G in the main context, with the findings in hand - they will inform
   both.
6. Propose the fix set to Paul, grouped, with a tier per group. **Do not fix
   during the audit.** Auditing and fixing in one pass is how a finding gets
   rationalised into a design change nobody agreed to.
7. On his go-ahead: fix, cover each with a self-test check shown to fail first
   where the change is behavioural, update `CHANGELOG.md` and `README.md` in the
   same commit, run the FULL suite, commit.

## Definition of done

`docs/audit-findings.md` exists and contains, for every finding: the claim, where
it is made, what actually happens, severity, and the proposed fix. Severity-1
findings are individually verified. F and G are answered in prose with a
recommendation Paul can accept or reject. **Nothing is fixed without his
go-ahead**, and the audit is complete whether or not any fix is made.

A finding of "the documents and the code agree" for an axis is a real result and
must be stated as one rather than padded.

## Where things are

| file | what |
|---|---|
| `overnight.py` | the runner, ~3,200 lines, one file |
| `selftest.py` | 23 sections, 292 checks, `--list` names them and their cost |
| `references/planning.md` | PLAN procedure - the file that carried defect 1 |
| `references/launching.md`, `progress.md`, `spec-format.md` | the other procedures |
| `SKILL.md` | the router Claude Code reads at session start |
| `docs/token-efficiency.md` | every measurement, including the session above |
| `docs/ROADMAP.md` | what is not built, and why each matters; item 14 is new |
| `docs/plan-v1.0.2.md` | the previous plan, and the five locked decisions |
| `CHANGELOG.md` | `[1.0.3]` is open; `[1.0.2]` and below are locked |
