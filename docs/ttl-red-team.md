# Red-team this: should an unattended agent runner use a 5-minute or a 1-hour prompt cache TTL?

Self-contained brief. You need no other context. **Please attack the reasoning,
not the presentation.** The author reversed his own conclusion once already while
writing it, so a second reversal is a welcome outcome, not an embarrassment.

## The system

A Python orchestrator runs unattended overnight batches of headless Claude Code
workers (`claude -p`). One worker per "step" of a plan. Each worker is a separate
OS process with its own conversation. A step is 10-25 minutes and makes 11-151
API calls; between steps the orchestrator runs shell "gates" (a test suite, a
lint) and commits, then launches the next worker. A night is 12-24 steps.

Prompt caching: each API call writes the new part of the conversation to a cache
and reads the rest back. Anthropic's list pricing, relative to base input tokens:

| | multiplier |
|---|---|
| cache write, 5-minute TTL | 1.25x |
| cache write, 1-hour TTL | 2.0x |
| cache read (either) | 0.1x |

TTL = how long an entry stays reusable. Claude Code chooses it. Since v2.1.242 an
operator can override with `promptCacheTtl` in `settings.json` or
`CLAUDE_CODE_PROMPT_CACHE_TTL`. Observed defaults: on a Claude subscription
within plan the main conversation gets 1h and subagents get 5m; on an API key
both get 5m; when a subscription's limit is exceeded the main conversation drops
to 5m.

## The measurement

Fourteen real workers across two projects on one day, parsed from the
`stream-json` logs. Totals, priced at list rates:

| bucket | cost | share |
|---|---|---|
| cache reads | $24.72 | 56% |
| cache writes (all at 1h, i.e. 2x) | $11.05 | 25% |
| output incl. thinking | $8.22 | 19% |
| uncached input | ~$0 | 0% |
| **total** | **$44.00** | |

The pricing arithmetic reconciles **exactly** with the `total_cost_usd` that
Claude Code self-reports, for 9 of 9 Opus workers. So the 2x figure for a 1-hour
write is empirically confirmed, not assumed.

The decisive split, which the author initially failed to look at:

- **1,550k tokens were written to cache in total.**
- **512k of those (33%) were first-call writes** - the fixed prefix, paid when a
  worker starts.
- **1,038k (67%) were intra-step incremental writes** - the conversation growing
  as the worker reads files and calls tools, written a few thousand tokens at a
  time, seconds apart.

Nine of fourteen workers started *warm*, reading 21-33k of their prefix from a
cache a previous worker had written, because the 1-hour TTL had kept it alive
across gaps of 17-65 minutes.

## The original (wrong) conclusion

"Keep the 1-hour TTL. It is the only reason a worker ever starts warm; the gaps
between steps are 17-65 minutes and a 5-minute cache would miss every one."

**Why that was wrong:** it optimised the 33% (first-call writes and cross-worker
warmth) and ignored the 67% (intra-step writes). Intra-step writes happen seconds
apart, so a 5-minute TTL survives them just as well as a 1-hour one - it is
simply 37.5% cheaper to make them.

## The revised conclusion

**Set the TTL to 5 minutes.** Expected saving ~6.5% of a run's tokens.

    all writes at 1.25x instead of 2.0x        -$4.14
    9 warm starts become cold: 226k tokens
      rewritten at 1.25x rather than read at 0.1x  +$1.30
    -------------------------------------------------
    net                                        -$2.84   (6.5% of $44)

The cross-worker warm start turns out to be nearly worthless: under a 5-minute
TTL a cold start writes ~55k at 1.25x ($0.34) where the 1-hour warm start wrote
28k at 2x plus read 27k at 0.1x ($0.29). Five pence a worker. The thing the
1-hour TTL buys costs more to buy than it is worth.

## Attack these

Ranked by how much damage they would do if true.

1. **The 5-minute intra-step assumption.** The saving depends on *every* gap
   between consecutive API calls within a worker staying under 5 minutes. If one
   gap exceeds it, that worker's entire context (up to 165k tokens here) is
   rewritten from scratch rather than extended - roughly $1.03 at 1.25x, which
   would wipe out the saving for that worker and more. Plausible causes: a slow
   tool call (a test suite; the ones measured took ~52s, but another project's
   could take 10 minutes), a long model thinking pass at high effort, an API
   retry or rate-limit backoff. **This is unmeasured.** The logs contain
   per-event timestamps, so the maximum inter-call gap per worker is computable
   and has not been computed. Is that the first thing to check? Is there a
   distributional argument (one bad gap in twenty workers is fine; one in three
   is not)?

2. **The 1.25x figure is not verified, only documented.** The 2.0x for a 1-hour
   write is confirmed by exact reconciliation against self-reported costs. The
   1.25x for a 5-minute write is taken from documentation and has never been
   observed in these logs, because every write in the sample was at 1h. If the
   real ratio were, say, 1.5x, the saving falls to about 3%. How would you verify
   it cheaply?

3. **The billing model may not apply at all.** These runs strip
   `ANTHROPIC_API_KEY` and authenticate as a Claude Max subscription. The dollar
   figures are what the tokens *would* cost on the API, used as a ranking unit.
   Whether subscription usage metering applies the same 2x/1.25x multipliers to
   cache writes, or meters raw tokens, or something else, is unknown. **If
   subscription metering ignores the multiplier entirely, the entire saving is
   illusory and the 1-hour TTL is strictly better** (same metered cost, more
   warm starts). How would you find out without an API bill to inspect?

4. **Anthropic chose 1h as the subscription default.** That is evidence they
   believe it is better for Claude Code's usage pattern. The counter-argument is
   that the default is tuned for *interactive* sessions, where a human types for
   minutes between turns and a 5-minute cache would expire constantly - a pattern
   an unattended runner does not have. Is that counter-argument sound, or is it
   the kind of "my case is special" reasoning that is usually wrong?

5. **Sample size and generalisation.** Fourteen workers, two repositories, one
   day, one machine, one Claude Code version (2.1.229; the machine has since been
   upgraded to 2.1.263, so even a repeat is not strictly comparable). Two models
   mixed (Opus and Sonnet). Is 6.5% inside the noise?

6. **Does the setting even reach the workers?** `promptCacheTtl` is read from
   `settings.json` by the Claude Code process. Each worker is a separate
   `claude -p` invocation; whether it inherits the user-scope setting, needs a
   project-scope one, or needs the environment variable set in the child's
   environment, is untested. The orchestrator already strips three variables from
   every child environment, so it can equally set one.

7. **Second-order effects nobody costed.** Would a shorter TTL change *behaviour*
   - more cache misses causing the model to re-read files it would otherwise have
   had in context? (The author believes not: cache state is invisible to the
   model and does not change what it sees, only what the tokens cost. Is that
   right?)

## What is not in dispute

- Cost is dominated by API calls multiplied by the context each carries: reads
  plus writes are 81% of spend, output only 19%.
- A headless worker costs the same *per call* as the same work done in an
  interactive session (measured against 22 interactive transcripts: 57/28/15
  against 56/25/19), so the overhead of running unattended is structural - the
  extra reviews, retries and reworks - not per-call.
- The largest single lever is what a tool result leaves behind in context, which
  is a prompting matter, not a caching one. TTL is worth ~6.5%; that is worth
  ~30%+ and is not what this document is about.

## The question

Given the above: **5 minutes, 1 hour, or is the honest answer "measure the
inter-call gap distribution first and decide after"?** If the last, what
threshold on that distribution should flip the decision?
