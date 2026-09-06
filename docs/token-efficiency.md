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

**3. Do NOT touch the cache TTL - but know what moves it.** Every cache write in
every log is at the 1-hour TTL (`ephemeral_1h_input_tokens`, with `ephemeral_5m`
at zero throughout), billed at 2x base input where a 5-minute write is 1.25x.
Forcing 5m would cut writes by 37.5%, about $4.14 of the sample's $44 - and it is
configurable, with `promptCacheTtl` in `settings.json` or
`CLAUDE_CODE_PROMPT_CACHE_TTL` (Claude Code 2.1.242 or later), plus the blunt
`FORCE_PROMPT_CACHING_5M=1`.

TTL is time-to-live: how long a written cache entry stays reusable before it is
discarded and the next call has to pay to write it again.

**Changing it would be a mistake, and it is not available here anyway.** The
1-hour TTL is the only reason a worker ever starts warm: the gaps between steps
in these runs are 17 to 65 minutes, every one of which a 5-minute cache would
miss. It also has to survive a worker's own slow gates mid-step. The gross saving
is real, the net is much smaller, and the risk is all downside. Separately, the
machine these runs were measured on is Claude Code **2.1.229**, below the 2.1.242
those settings need, so the knob does not exist here until an upgrade.

Two things the operator should know instead, because neither is under the
runner's control and both change the bill:

- **On a Claude subscription within plan, the main conversation already gets 1h
  and subagents get 5m.** On an API key or usage credits, everything gets 5m.
  **And when a subscription's limit is exceeded, the main conversation silently
  drops to 5m too** - so a run late in a billing period has a different cost
  profile than the same run early in one, with no signal in the log.
- The 5-minute default on subagents is one more reason the subagent lever in
  item 2 is narrower than it looks.

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
  does, conditionally - see the section below, which is worth reading because the
  conditions are not obvious.
- **Whether the v1.0.1 nudge changed behaviour.** Not separable from the sample:
  the note landed after these runs, and the workers that used PowerShell heavily
  versus Bash heavily differ by project convention, not by the note. Re-measure
  with `tools/tally.py` after the next run rather than reasoning about it.

## Separate workers do share cache, but the key is narrow

Each worker is its own `claude -p` process, so it is fair to ask whether any of
them start warm. They do, and the evidence is each worker's *first* API call: a
cold one writes 51 to 61k tokens and reads nothing, a warm one writes 26 to 29k
and reads 21 to 33k. Same total prefix either way, about 55k; the difference is
how much of it had to be paid for again.

Claude Code's cache key includes the **model**, the **effort level**, the
**working directory**, the tool definitions and a git-status snapshot. Laid
against the FinKit run in start order, that explains almost all of it:

| start | step | model/effort | first call | why |
|---|---|---|---|---|
| 10:11 | 3b-ref-params a1 | sonnet/med | cold | first of its key |
| 10:28 | 3b-ref-params a2 | sonnet/med | **warm** | 17 min later |
| 10:42 | reflect-0 | opus/**high** | cold | first opus/high |
| 10:48 | 3b-exempt-balances | sonnet/med | **warm** | 20 min |
| 13:40 | 3c-flow-denominator | sonnet/med | cold | 2h24 gap, expired |
| 13:49 | 3c-annual a1 | opus/**med** | cold | first opus/med |
| 14:08 | review:3c-annual | opus/**high** | cold | 3h26 since reflect-0 |
| 14:27 | 3c-annual rework | opus/med | **warm** | 38 min |
| 15:32 | 3c-rate-conversion | opus/med | **warm** | 50 min |
| 15:52 | 3c-components | opus/med | **warm** | 20 min |
| 16:09 | 3b-diff-classifier | sonnet/med | **warm** | unexplained: 2h20 gap |

**The effort level is what separates a review from a build.** The review at 14:08
started cold nineteen minutes after a build of the same model, because
`defaults` gives builds `medium` and reviews `high`, and those are two caches. A
run that alternates build and review is running two cache namespaces, not one.
The last row is not explained by any of this and is recorded as an anomaly rather
than smoothed over.

**What it is worth.** A cold Opus start costs about $0.55 against $0.29 warm, so
roughly $0.26 a worker. Five of eleven FinKit workers started cold, about $1.30
on a $46.52 run - under 3%. Worth understanding, because it explains cost
variance between runs that otherwise look identical. Not worth engineering
around, and certainly not worth flattening the tier defaults for: a review is set
to `high` because the judgement is the point.

One consequence does deserve recording, because it was not known when the
decision was taken: **worktree isolation puts every build step in a different
working directory, and the working directory is part of the cache key.** The
warm starts above show the effect is only partial, so some cached segment
survives the move - but isolation was made the default on correctness grounds,
and it carries a cache cost nobody priced. It is still the right default.

## Effort, and step length

Two levers that look promising and are not, plus the one inside them that is.

**Lower effort saves less than it appears, because output is only 19% of the
bill.** The two `high` workers spend about half their output on thinking - the
reflect 51%, the review 51% - against 18 to 30% for the six `medium` builds, and
they are the two most expensive calls in the sample at $0.14 and $0.12 against
$0.06 to $0.09. But review and reflect together were only 12% of the run, and
halving their thinking would save well under 1% of it. The kinds also differ, so
effort is not cleanly separated from the work being harder.

The indirect effect is the one that could matter and is unmeasured here: lower
effort also means fewer and more consolidated tool calls, and calls are 81% of
the bill. That is worth a controlled test - one step, run at `medium` and at
`low`, compared with `tools/tally.py` - and not worth assuming.

**Where to actually spend this:** per-step `effort` is already supported by the
runner, so no code change is needed. A mechanical build step does not need
`medium`; one schema-authoring step in the sample spent 68% of its output on
thinking to write a file that was largely specified for it. Setting `effort: low`
on obviously mechanical steps at plan time is free and available today. Do not do
it to review steps: review is where the run's value showed up, and `high` is
there because the judgement is the point.

**Longer steps are worse, not better.** The intuition is that a longer step
amortises the fixed prefix over more work. The data says the prefix is the small
term and context growth is the big one: mean context per call rises with step
length, from 50k over 11 calls to 165k over 151. Cost rises faster than the work
does - a 53-call step cost $3.61 and a 100-call step $8.82, so merging two of the
former into one of the latter would cost about 20% more and save one prefix worth
$0.30. The 151-call step carried the highest mean context in the sample.

The counter-pressure is real but is not a token cost: every extra step means
another gate run in wall clock, and a fresh worker re-orients by re-reading files
its predecessor had already read. So there is an optimum rather than a direction,
and the sample sits near it. **The evidence does not support lengthening steps,
and the timeout is a separate question - a timeout that is never hit costs
nothing, so raising it for safety is free.**

### So should a long step be split into several short ones?

Sometimes, and the mechanism says exactly when. A worker's context starts at the
prefix and grows as it reads, so its total spend has two terms: **the prefix,
paid once per call, and the growth, paid roughly as the square of the call
count.** Splitting a step attacks only the second term - and adds a third, the
re-orientation reading each fresh worker does to catch up.

That gives a U, and the sample shows it. Cost per call by step length:

| calls | $/call |
|---|---|
| 11 | 0.079 |
| 22 | 0.060 |
| 27 | 0.074 |
| 53 | 0.068 |
| 69 | 0.080 |
| 100 | 0.088 |

Both ends are dear. An 11-call step is dear because a 55k prefix is amortised
over eleven calls; a 100-call step is dear because its context grew all the way
to 119k. **The floor is around 20 to 55 calls, which is roughly the 7 to 15
minute step this project already recommends.**

So: split a step that is genuinely long - the 100-call, 30-minute kind, where the
quadratic term dominates and `expected_min` has already flagged it as carrying
more than one deliverable. Do not split a 15-minute step into three 5-minute
ones: that lands on the left arm of the U, pays three prefixes instead of one,
and adds two rounds of re-orientation reading, on top of three gate runs and
three worktree cycles.

**And splitting cannot be justified by the cache TTL.** A 5-minute TTL is not a
prize for finishing quickly - it is cheaper to *write* (1.25x against 2x) and
expires sooner, and it applies to every write the process makes, not to the step
as a unit. Within a step the calls are seconds apart, so any TTL survives them
regardless of how long the step runs. What a short TTL would break is the gap
*between* workers, which is set by gate runtime and by what else the run
interleaves - 19 minutes in the sample - and which shortening the steps does not
shorten.

## A note on the arithmetic, and on what the dollars mean

**These runs do not bill an API account.** The runner strips `ANTHROPIC_API_KEY`
from every worker's environment so the child authenticates as the subscription,
and the workers are `claude -p` (`--print`, the headless form - there is no `-u`
flag). So the dollar figures throughout this document are **not a bill**. They
are what the same tokens would have cost at list price, used as a common unit so
that buckets and steps can be ranked against each other. What the subscription
actually meters, and whether it applies the same 2x multiplier to a 1-hour cache
write, is not something these logs can show. **The token counts are the
measurement; the dollars are the ranking device.** That does not affect any
conclusion here, because every one of them is a comparison.

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
