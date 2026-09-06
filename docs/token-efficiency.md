# Where an overnight run's tokens actually go

Measured 2026-09-06 over every real worker this project has ever run: eleven
FinKit workers (`stage-3b-5b`) and three Woodwork Guru workers (`engine-b0-b8`),
against twenty-two interactive Claude Code sessions from the same three days as
the control. `tools/tally.py` produced every number here from the logs the
runner and the harness already write; nothing was estimated.

The objective this serves is in `CLAUDE.md`: an unattended run must not be
significantly more token-hungry than doing the same work interactively. Some
overhead is the price of nobody being awake. 2x is not acceptable.

## The one-line answer

**Cost is the number of API calls multiplied by the context each call carries.**
Everything else is noise. Over the fourteen workers, at list price:

| bucket | share |
|---|---|
| context re-read on each call (cache reads) | 56% |
| cache writes | 25% |
| output, thinking included | 19% |
| uncached input | 0% |

A worker makes one API call per tool call, and every call re-reads the whole
conversation so far. A worker that makes 151 calls at a mean context of 165k
tokens reads 24.6M tokens to do one step. That is the bill. The brief, the
tool-usage note, the summary table and the gate list are all in the noise.

## Headless is not the problem

The control matters, because it is the thing the objective is written against.
Twenty-two interactive FinKit sessions from the same days show the same profile:
mean context per call 90k to 183k, and a bucket split of 57 / 28 / 15. The
overnight workers sit at 50k to 165k mean context and 56 / 25 / 19. **A headless
worker costs the same per call as a person driving the same model by hand.**

So the 2x risk does not live in the brief or the harness. It lives in the extra
*work* an unattended run does: attempts that get discarded, reviews, reflects and
reworks. On the FinKit run those came to:

| | cost | share of the run |
|---|---|---|
| review + reflect + rework | $8.12 | 17% |
| attempts discarded by the `clean_tree` defect | $3.98 | 9% |
| the run | $46.52 | |

The first number is the honest price of running unattended, and it bought two
real bugs the build's own gates had missed. The second was a defect, since fixed
by worktree isolation. Inside tolerance, and worth watching per run rather than
assuming.

## What is worth changing, in order

**1. What a tool result leaves behind.** This is the lever with no downside.
Read results alone put 769 KB into worker contexts, and the single largest was a
51 KB file read whole. A file read in full at call 10 is still being paid for at
call 100 - that is what makes context re-reads 56% of the bill. Grep with
context, and Read with an offset and a limit, answer the same question for a
fraction of what they leave behind. This is a brief-level instruction, and unlike
the general "prefer Read over Bash" nudge it names the actual failure. Nothing
about it can misfire: a worker that reads less carries less.

**2. A subagent for orientation reading only - and the case is narrower than it
looks.** Every worker is handed the `Task` tool, confirmed in the harness init
event of a real worker log, and across all fourteen workers it was called zero
times. But a subagent does not make the reading cheaper. It does the same reading
in its own context, pays its own 20-28k cold prefix on top, and hands back a
summary. **The only thing it saves is what the reading would have left behind in
the parent for every later call.**

That saving is real but conditional, and both conditions must hold:

- **The worker needs the conclusion, not the contents.** "Where is X handled",
  "which tests cover Y", "does this pattern appear anywhere else" - those are
  delegable. Reading a file in order to *edit* it is not: `Edit` needs the
  contents in the parent, so delegating that read saves nothing and costs a
  second prefix.
- **The step is long enough for the pollution to be carried.** Forty orientation
  reads early in a 150-call step are paid for 110 times over. The same reads in a
  20-call step are barely paid for at all, and a subagent there is a straight
  loss.

And the failure mode is expensive: **a subagent that fans out in parallel
multiplies usage rather than dividing it.** Several agents, each with its own
prefix and its own growing context, each thinking, can cost more than the inline
reading they replaced. So the brief must say *one* subagent, for orientation, when
the answer is a summary - not "use subagents to explore". Worth a bounded
experiment on one real step before it goes into every brief, measured with
`tools/tally.py` against the same step run without it, not adopted on the
argument alone.

**3. The cache TTL.** Every cache write in every log is at the 1-hour TTL
(`ephemeral_1h_input_tokens`, with `ephemeral_5m` at zero throughout). A 1-hour
write costs 2x base input where a 5-minute write costs 1.25x. Across the sample
that difference is about 9% of the total bill. **Not a recommendation yet**: a
step whose gates take ten minutes would miss a 5-minute cache and pay far more
than it saved, and whether the runner can influence the TTL at all is unverified.
Worth one look at the Claude Code documentation before anything is changed.

**4. The tool list, for safety first and tokens second.** A worker is given
`Artifact`, `CronCreate`, `CronDelete`, `PushNotification`, `RemoteTrigger`,
`SendMessage`, `Workflow`, `DesignSync` and more. An unattended worker has no
business publishing a web page, creating a scheduled job or sending a message to
another session, and `--disallowedTools` already exists in `worker_argv` for
review steps. The token argument is secondary and small - the fixed system prompt
and tool definitions are 20k to 28k tokens of every context, so trimming a third
of it saves perhaps 4% - but the safety argument stands on its own.

## What was investigated and is not worth changing

These were the open questions in the pass this replaces. Each is answered by the
measurement, and the answer is no.

- **Repeating the full brief on attempts 2 and 3.** A brief is 5 to 17 KB, about
  2 to 4k tokens, against contexts of 80k to 165k. Under 2%, and a diff instead
  of the brief risks a worker acting on half a specification.
- **Compacting the rework brief.** The largest rework brief in the sample was
  15 KB. Same arithmetic.
- **A rolling window on the reflect step's summary table.** The reflect worker's
  brief was 17 KB against a 121k peak context, and the table was a fraction of
  it. It would need a plan several times longer than any run so far to matter.
- **Pre-computing `git show --stat` for the reviewer.** The reviewer ran it
  itself for 15 KB. Pre-computing saves one call out of 27.
- **Tailoring the tool-usage note per worker kind.** The note is 300 bytes. Per
  kind it would still be 300 bytes.
- **Whether prompt caching survives across separate `claude -p` processes.** It
  does. Cold first calls write 22 to 30k tokens; later workers in the same run
  read 21 to 33k from cache on their *first* call, so the fixed prefix is shared
  across processes. Nothing to fix.
- **Whether the v1.0.1 nudge changed behaviour.** Not separable from the sample:
  the note landed after these runs, and the workers that used PowerShell heavily
  versus Bash heavily differ by project convention, not by the note. Re-measure
  with `tools/tally.py` after the next run rather than reasoning about it.

## A note on the arithmetic

`tools/tally.py` prices tokens at list rates and reconciles **exactly** with the
`total_cost_usd` every Opus worker reports - nine of nine. The Sonnet workers
report about 1.5x the tool's figure. That is consistent with a long-context
premium above 200k tokens, which is where those workers were sitting, but it has
not been verified and the tool does not model it. Treat Sonnet rows as a floor.
The bucket *shares*, which is what the ranking above rests on, are unaffected.

## Re-measuring

    python tools/tally.py overnight/runs/<run name>
    python tools/tally.py ~/.claude/projects/<slug>/<session>.jsonl

The same tool reads both a worker log and an interactive transcript, so the
comparison that this document rests on can be repeated on any project at any
time. Do that after a run rather than trusting these numbers to age well.
