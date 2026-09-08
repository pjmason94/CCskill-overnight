# Changelog

All notable changes to this project are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.3] - unreleased

### Changed

- **Build steps now default to `sonnet/medium` instead of `opus/medium`.**
  Measured over one real 27-worker run, same project and comparable context:
  sonnet cost **$0.051 an API call against opus at $0.090**, and nothing in that
  ledger made sonnet the weak link - the two steps that burned a second expensive
  attempt (`$14.53` and `$7.38`) were both opus, and so were both steps a review
  sent back for rework.

  **Review, reflect and diagnostic stay `opus/high`, deliberately.** The reviewer
  is the compensating control for whatever the builder missed, so downgrading
  both at once removes the thing that made the first downgrade safe. The
  arithmetic agrees: reviews were 14% of that run's cost, and cutting them to
  medium would save about $7 on a run whose build cost is falling by $50 anyway.
  The saving belongs in the thirty build steps, not the six reviews.

  Set `defaults: {build: {model: opus, effort: medium}}` in the spec to restore
  the old behaviour for a plan that wants it, or `model:` on the step itself.
  Reach for `sonnet/high` before `opus/medium` when a build carries real design
  choice - the middle rung was previously skipped entirely.

  **Existing plans that set their own tiers are unaffected.** A plan that says
  nothing about tiers will build with sonnet from now on, so size
  `budget_usd_per_step` against the opus figures in README section 14 until a
  sonnet-default run has been tallied.

- **Every brief now names what the step FEEDS, not only what feeds it.** The
  `## Read first` half was already the convention and was written; the downstream
  half was missed everywhere, and it is the one that pays. Without it a worker
  that finds an upstream signature inconvenient renegotiates an interface nobody
  fixed - on one real run a step reached back and edited the module upstream of
  it so its own work would fit, and the integrating step downstream then had to
  reconcile two designs each built correctly against different assumptions.
  Naming what a step feeds holds it to its objective while it runs, and is the
  contract the integrating step is later assembled from. A step with no consumer
  yet must say so rather than omit the section, because an absent section and a
  deliberate "nothing consumes this yet" are otherwise indistinguishable.

- **The planner is told to cut every step to fit `sonnet/medium`, and to justify
  anything above it in one sentence.** Wanting a stronger model is usually the
  same signal as an over-long estimate - the step carries more than one decision -
  so splitting is tried before escalating, `sonnet/high` is the next rung rather
  than `opus/medium`, and ceiling and deliberation are named as different axes.
  An escalation nobody has to justify is one that spreads to every step.

- **Every build worker is now asked to re-read its own diff before committing.**
  One `git diff` at the end, read as though somebody else had written it, looking
  for the class of local mistake that is invisible while writing and obvious
  afterwards: an inverted condition, an off-by-one, the wrong one of two similar
  names, a half-adjusted copy-paste, a debug print left behind. Deliberately one
  call at the end rather than a habit of re-reading as it goes. It exists because
  the review step that follows sees the design and rarely catches this class, and
  the gate only catches what a test happens to cover.

- **The README now says plainly that a step's gate is never the full suite**, and
  what to do instead. A 10-20 minute suite run after each of twenty 15-minute
  steps is three to seven hours of a night spent re-proving finished work. Gate a
  step on the test node ids it created; put the full suite in a `kind: gate`
  checkpoint every few steps, which costs no tokens and bounds which steps could
  have broken it. Both forms already worked.

  **`references/planning.md` said the opposite** and had to be corrected: it
  instructed the planner to append "the whole suite" to every build step through
  `run.gates`. The cost was mandated rather than merely undocumented.

- `docs/token-efficiency.md` gains two sections. **Two methodologies, measured
  against each other**: an overnight run and a long interactive Opus session on
  the same codebase in the same week, where the long session cost ~2x per API
  call and per code line, spent 69% of its night idle having run out of plan, and
  where the runner's own reflect steps went barren mid-cascade because an
  in-band judgement step shares a failure mode with the outage it is meant to
  catch. And **Assessing the code itself**: why cyclomatic complexity and the
  Maintainability Index mislead as depth measures, and what to use instead.

## [1.0.2] - 2026-09-08

### Added

- **`/overnight plan <path>` plans from documents you have already written.**
  Name a roadmap, an issue list, a design note or last run's
  `DECISIONS-PENDING.md`, and the planner reads them, proposes the step list from
  them, and interviews only on what they did not answer. A user who has already
  written down what they want should not have to say it twice.

  It takes a **path, not a format**. Requiring a shape - a heading per step, a
  bullet per gate - would mean that by the time somebody had written it they had
  written `steps.yaml`, and the project would then carry two documents to keep in
  sync, which a reflect step rewriting the plan mid-run makes worse. Any prose
  you already have is a legitimate seed.

  It does not save you the gates. A roadmap says what should be true and almost
  never how you would know it is; turning that into a command that exits 0 is the
  slow half of planning and the half that makes a run unattendable. The seed
  saves the enumeration, not the judgement.

  **The planner must list what it dropped, and why, at the top of its proposal** -
  and the same for anything it merged or split. A seed that is silently filtered
  is worse than no seed, because you handed over a document and believe it is the
  plan; three of its eleven items quietly missing is something you would discover
  in the morning, having reviewed a plan you thought you already knew.

  Documentation only - `SKILL.md` and `references/planning.md`. No runner change,
  and so nothing here can affect a run in flight, and no self-test covers it.

- **`expected_min` is required on every build step still to run, and the clock
  now uses it.** A plan with an unsized build step is refused at load, naming
  every offending step so one edit fixes it. Steps that have already run are
  exempt - their actual is recorded, and rewriting history to satisfy a new rule
  teaches nobody anything.

  The refusal is deliberate and it is placed at planning, not execution: a step
  nobody can size is a step nobody scoped. Across one 36-step plan, every build
  step carrying an estimate finished in 8-24 minutes and every step carrying none
  ran 28, 33, 52, 53, 54, 119 and 151. Writing a number does not make work
  faster - it is evidence the work was understood well enough to hand to a worker
  alone.

  With every step sized, the clock can schedule. A build step that cannot finish
  before the stop time is not started: it records `NOT RUN` with both figures in
  its note - the estimate and the minutes left - so it reads as a clock decision
  and not as a usage-wall casualty, the other thing that produces `NOT RUN`. The
  runner then takes the next step that does fit. When nothing left fits, the run
  stops and says the remaining steps are pending, not failed. Reviews are never
  declined for the clock: leaving a passing build unreviewed until somebody
  relaunches costs more than running a few minutes over.

  **Skipping ahead assumes the passed-over step was not a prerequisite of the one
  that runs instead, and nothing verifies that.** The plan carries no record of
  what feeds what. Stopping the run was the alternative and it is worse - it
  throws away the rest of the night over a hazard that can be stated plainly
  instead - so the runner states it twice: in the log at the moment the decision
  is taken, and at the top of `SUMMARY.md`, naming which step was skipped and
  which ran in its place.

  New self-test section 25, twenty-three checks.

- **The clock is a time of day now, not a duration.** `--until 07:30` stops the
  runner STARTING new steps at the next 07:30 - tomorrow's, if today's has
  passed - and `--until "2026-09-08 07:30"` names one exact moment. `run.until`
  sits in the spec beside `run.hours`. A step already running is still never
  interrupted by the clock.

  `--hours` asked the operator to convert the constraint they actually have -
  "results by 07:30" - into a duration, in their head, last thing at night. The
  conversion then decayed: the minutes between computing it and pressing return
  came off the end of the run, and a scheduled launch that fired late, or a
  relaunch after a refusal on a dirty tree, overshot the morning by exactly the
  delay - silently, because nothing in the run knew what time had been meant.

  Four sources can set the clock, resolving `--until` (CLI) > `--hours` (CLI) >
  `run.until` > `run.hours`. The command line beats the spec first, and only
  then does `until` beat `hours` within a level, so an `until` sitting in the
  plan file can never make a typed `--hours` inert. The banner and the `STOP:`
  line both name which source won and the absolute time it resolved to. A dated
  stop time already in the past is refused rather than rolled forward a day,
  with a sentence rather than a traceback.

  New self-test section 24, twenty checks. **The clock had no test at all before
  this** - and it is the one path that can end a run with steps still pending
  and no failure to show for it.

- **The self-test can now run part of itself.** `python selftest.py --list` names
  the twenty-two sections, what each needs before it, and how many checks each makes.
  `--only 13,17` runs those plus anything they depend on; `--from 17` runs the
  rest of the suite. A partial run prints `SELFTEST PARTIAL OK` and **never**
  `SELFTEST PASS`, so it cannot be mistaken for the gate a launch requires.

  The suite runs to twenty minutes, which is not a test loop, and the honest
  consequence of paying it on every iteration is that it gets skipped. Fifteen of
  the sections already built their own repository and stood alone; the four
  that read a fixture an earlier section left in a particular state now declare
  it, and asking for one of those runs its prerequisites too rather than quietly
  running a subtly different test. A run also ends with a table of where its time
  went, section by section, which is what makes `--only` worth aiming.

- **Every self-test section declares how many checks it makes, and the suite fails
  if it makes a different number.** The hazard in selecting sections is silent: a
  section dropped from the table, or a check lost inside one, still ends in
  `SELFTEST PASS` while the suite gets quietly weaker. The declared counts turn
  that into a loud failure naming the section and the number it actually made.

- **A cut-off worker can now be CONTINUED rather than repeated, and a cap can be
  sized to a step.** Two new keys: per-step **`budget_usd`**, which overrides
  `run.budget_usd_per_step`, and **`run.continuations`** (`--continuations`,
  default **0**, off).

  Time has always been per-step (`timeout_min`); money was not. One run-level cap
  has to be sized for the plan's *largest* step, which leaves every smaller step
  effectively uncapped and means a trip could never say anything useful about the
  step it happened on. `budget_usd` is the matching knob.

  With a cap that fits the step, being cut off stops being an anomaly and becomes
  a schedule. So **one attempt may now take several workers**: when the budget
  cuts a worker off part-way, the next one gets a fresh cap and the tree exactly
  as the last left it, and a handover brief telling it what is already built and
  not to start over. That is the whole difference from a retry - the same money
  buys *progress* instead of *repetition*.

  **A worker that changed nothing is never continued.** The runner fingerprints
  the tree (HEAD plus everything uncommitted) either side of each worker. One
  that spent an entire cap and left the tree byte-identical is a runaway, not a
  big step: there is nothing to hand on, and handing a fresh worker a half-built
  wrong thing to finish is worse than stopping. That trips `OVER BUDGET`
  immediately, and so does exhausting the continuation limit - without a bound
  this would be the same money pump the retry was.

  The undo model is unchanged where it matters: the tree is carried forward
  *between legs*, and still reset when the attempt ends, so "a failed attempt
  leaves no trace" still holds at the attempt level. The ledger records `legs:`
  when a step took more than one worker, and each continuation's log is kept
  beside the first as `attempt-N-continued-M.log`.

- **A step that runs out of budget now stops instead of buying the same failure
  three times.** `run.budget_usd_per_step` has always been passed to each worker
  as `--max-budget-usd`, but a trip was just another failed attempt: the cap is
  per *invocation*, so a $6 cap could bill $18 over three attempts and be cut off
  in the same place each time. A build attempt whose worker ends with
  `subtype: error_max_budget_usd` is now recorded as the new outcome **OVER
  BUDGET** and is not retried, and no diagnostic is run over it either - a
  diagnostic would be asked to explain a gate failure whose only cause is that
  the worker never got to finish.

  It is treated as a **planning** failure, not a finding about the code: either
  the brief asks for more than the cap will pay for, or the cap is set below what
  the work costs, and only a person can say which. So `OVER BUDGET` is blocking
  (`--mode` prints `BLOCKED`, and the summary flags the step `<-- NEEDS YOU`) as
  well as resumable, the same pair `STUCK` and `HALTED` have: a resume picks it
  straight back up, but only once somebody has split the step or raised the cap.
  The note names both figures - `worker RAN OUT OF BUDGET ($6.02 against a $6.00
  cap)` - so the morning can see how far off the cap was without opening a log.

  **The gates remain the arbiter.** A worker that commits passing work and only
  then runs out of money on the tidying up is still a `PASS`: the trip is read
  before the gates run and acted on only if they fail, so work that is on the
  branch is never thrown away for an exit code.

- **A stall watchdog: a worker that has stopped writing is killed, not paid for
  to the timeout.** `run.stall_min` / `--stall-min`, default **10 minutes**, 0 to
  disable. On 2026-09-07 two workers went silent with their log frozen and each
  burned the full 90-minute `worker_timeout_min` before `exit 124`; the retries
  then passed in 27 and 47 minutes, so the retry was always the fix and the only
  cost was the waiting - about two and a half hours of one night.

  `worker_timeout_min` cannot do this job. It has to be set for the slowest step
  the run legitimately contains, so it can never catch a stall early: it is a
  backstop against a runaway, not a detector of a stopped process.

  **A slow worker is not a stalled one, and the runner can tell.** Both slow paths
  keep the log growing - a Bash call lasting over ~30s emits a `tool_progress`
  heartbeat every 30s, and a long generation emits `thinking_tokens` records
  throughout - so a log that does not grow at all means neither is happening. The
  default is measured rather than guessed: across 25 real worker logs the largest
  silence a *working* worker ever produced was **291 seconds**, with a median
  per-log maximum of 81s, so 10 minutes is about twice the worst case observed.
  The log size is now sampled every couple of seconds rather than once a minute,
  so detection is not quantised to the heartbeat.

  A stall is recorded as a spent attempt and the step is retried, because that is
  what actually fixed it in the field. The attempt note says `worker STALLED`
  (and, for the timeout, `worker TIMED OUT`) so the morning can tell a worker that
  never ran from one that tried and got it wrong. Exit code 125 is excluded from
  the barren count alongside 124: a stalled worker exits non-zero with no result
  event, which is exactly the shape of a barren one, and three stalls must not be
  read as a usage wall.

  The watchdog cannot fire during a gate - it lives in the worker's own heartbeat
  loop, which only runs while a worker does.

### Changed

- **A reflect worker is now told that a new build step needs `expected_min`**,
  and that a step it cannot size is one it should split. Without the estimate the
  rewritten plan no longer loads, and the runner reverts the whole rewrite.

- **Work a crashed run left on a scratch branch is now RETESTED, not thrown
  away.** A crash can land after a worker has committed and passed its gates but
  before the run integrated it, leaving a finished step on its scratch branch and
  nowhere else. The runner already tagged those commits before re-running the
  step, which kept them recoverable - but the morning was told nothing, and the
  natural next move rebuilt work that already existed.

  Before a build step runs, if its scratch branch holds commits that are not on
  the operator's branch, the runner now replays them onto the branch's current tip
  and puts them through that step's own gates. They pass, and the work is merged
  exactly as a successful re-run's would be: the step records `PASS` with
  `attempts: 0`, a note saying where the work came from, the gate output kept as
  `stranded.log` - and **no worker is spawned**. They fail, and they are tagged
  and discarded exactly as before and the step runs normally. They will not
  replay, and that is `NEEDS MERGE`, which already meant precisely this.

  Reporting it and leaving the operator to decide was the weaker answer. The gate
  is the arbiter of whether work is good everywhere else in this runner, so it is
  the arbiter here. The replay is what makes the test honest - the stranded commit
  was built against an older base, and gating it where it was built would prove
  only that it used to work.

  The case this comes from: a worker finished at 02:32:39, exit 0, 11.3 minutes,
  $5.12, gates passed. The relaunch recorded `STUCK, attempts: 0, could not create
  the worktree for this step`, which reads as *nothing happened*. Under this
  change that step costs nothing at all to recover.

- **Planning now calibrates against what the project actually recorded.**
  `references/planning.md` already required one deliverable per step at about
  fifteen minutes; what it lacked was any instruction to look at a previous run's
  numbers. It now reads the `done:` blocks and the estimate-vs-actual table in
  `SUMMARY.md` as part of reading the project, and cuts new steps beside those
  actuals.

  The finding that drove it, measured over a real 36-step run: every build step
  carrying an `expected_min` finished in 8 to 24 minutes, and every step carrying
  none ran 28, 33, 52, 53, 54, 119 and 151. Writing an estimate does not make a
  step shorter - **a step nobody sized is a step nobody scoped**, so an unsized
  step is the reliable predictor of an overrun.

- **Re-cutting an existing plan is now documented.** New steps take new ids, so a
  stale `done:` is left behind with the step it belonged to and the replacements
  start clean - rather than reaching for `--reset-state`, which forgets every
  outcome in the plan including the ones worth keeping.

- **A re-plan is now allowed as a resolution to BLOCKED, at the user's
  instruction only.** BLOCKED still reports first and the skill still must never
  *offer* to re-plan - a blocked step is a finding, and planning it away unasked
  hides the thing the user needs to see. But a step blocked because it was cut
  too big has a legitimate answer that is not a command, and previously the skill
  stopped dead rather than letting the user take it.

- **The console heartbeat is every 5 minutes, not every 10.** `run.log` still gets
  a line every minute; only the echo changed. Ten minutes was too coarse to watch
  a run by - a build step is planned at about fifteen, so the console showed one
  heartbeat before the step was even due to finish, and could not distinguish
  "nearly done" from "already at twice its estimate". Display only; no test covers
  it, because there is no behaviour to assert beyond the cadence itself.

- **Workers are now told to keep tool commands short-running and scoped**, not
  just to keep their results small. The note already covered output size; it said
  nothing about how long a command takes. Measured on FinKit `5b-recognise`:
  **1,411 seconds - 23.5 minutes of a 47-minute step - was spent inside tool
  calls**, roughly half its wall clock. Workers are now told to run the narrowest
  thing that can fail (a test node before a file, a file before the suite), that
  they never need to run the whole suite to prove they are done because the run's
  gates do exactly that afterwards, and not to repeat unchanged a command that has
  already taken more than about two minutes.

  This is aimed at the wall clock rather than the bill - a tool call's *duration*
  costs no tokens - with one exception that does cost: a command that outlives the
  prompt cache makes the turn after it re-read the whole accumulated context at
  full price instead of a tenth of it.

  Deliberately a standing rule and not a per-call estimate. An estimate asks for a
  prediction the model cannot make reliably, spends output tokens on every call,
  and then sits in the context being re-read for the rest of the step.

### Fixed

- **The published self-test runtime was wrong by a factor of thirteen.**
  `README.md`, `SKILL.md`, `CLAUDE.md` and the module docstring all said the
  self-test ran "in under a minute". Five runs on 2026-09-07 measured between
  **6.8 and 19.0 minutes** over 235 to 292 checks - and the spread is the
  machine rather than the checks, since the fastest run was uniformly faster
  across sections that had not changed. It is real
  subprocesses doing real git work, and it has grown with every section added
  since that claim was first written. A run now ends with a table of where its
  own time went, so nobody has to take a documented figure on trust again.

  A wrong number is worse than a bug, because nobody goes looking for it: anybody
  who budgeted a minute for the pre-launch gate was budgeting for the wrong thing,
  and the likeliest response to a thirteen-minute wait they were not expecting is
  to assume it has hung. All four places now carry the measured figure. (The
  `[1.0.0]` entry below still says "about two minutes"; a released section is
  never edited, and it was wrong when it was written.)

- **A worker woken by its own background task is now counted once, in full.** A
  worker that backgrounds a command gets woken when it finishes, and a second
  session is appended to the same log: one process, one log, **two `result`
  events**. Both the runner and `tools/tally.py` read only the last one. FinKit
  `5b-recognise` was therefore logged as `turns=4 $14.53` for a step that really
  took 123 turns, and `tally.py` costed its 4-turn tail alone - **$0.54 against a
  real $14.53**, a 27x under-count, which is worse than a crash because nobody
  goes looking for a number that merely looks small.

  The fields do not accumulate alike, and this was measured rather than assumed:
  `usage`, `num_turns` and `duration_ms` are **per-session** and are now summed;
  `total_cost_usd` is **cumulative over the process** and is taken from the last
  result, because summing it would have double-counted that step to $28.52.
  `tally.py` now reconciles exactly - $14.53 computed against $14.53 reported,
  where it previously printed $0.54.

  `docs/token-efficiency.md` is unaffected and was not re-measured: of the 144
  real worker logs on this machine only one holds more than one result event, and
  it postdates that sample. A note in the file says so.

- **A leftover worktree no longer costs a step its night.** `prune_worktrees`
  compared `str(path)` against the text of `git worktree list --porcelain`. That
  listing prints **forward** slashes on every platform; `str(path)` on Windows
  prints **backslashes**. The membership test was therefore false for every
  registered worktree, and the function deleted the directory of all of them -
  live ones included. `git worktree prune` had already run by that point, so each
  deletion left a *dangling registration*, and the next `worktree add -B` for that
  branch failed `already used by worktree`. The step was recorded `STUCK` with
  **zero attempts**, having read no code and spent nothing; on 2026-09-07 that
  took a real run's step out for the night in 75 seconds.

  Paths are now compared resolved and case-normalised, never as text, so only a
  directory git genuinely does not know about is treated as an orphan; `git
  worktree prune` runs again after any removal; and `add_worktree` prunes and
  retries once if it still meets `already used by worktree`. If git cannot list
  its worktrees at all the runner now leaves the leftovers alone and says so,
  rather than reading "cannot say" as "there are none".

- **Work a crashed run left on a scratch branch is now rescued before the branch
  is reset.** `worktree add -B` force-moves the step's scratch branch to the base
  commit. A run that crashed mid-step has its only copy of that work committed
  there and nowhere else, so re-running the step made those commits unreachable -
  no tag, no note, nothing for the morning to find. They are now tagged
  `rescue/<step>/scratch-<n>`, logged with their subjects, and listed in the
  step's `discarded-commits.md` with the `git cherry-pick` to get them back. This
  is the rule `safe_reset` has followed since the beginning, applied at the other
  place the runner moves a ref.

- **A character the console could not print no longer ends the run.** The runner
  echoes worker-written text to stdout, and a worker's summary carrying `<=`
  killed a live 02:00 scheduled run on 2026-09-07: Task Scheduler's `cmd.exe`
  runs at cp1252, Python's default `strict` errors raised `UnicodeEncodeError`
  inside `Log.__call__`, and the exception unwound the whole run - *after* the
  worker had finished and committed, but *before* the outcome was recorded. The
  work survived orphaned on its scratch branch while the plan said nothing had
  happened, which is the worst shape a failure can take: the morning cannot see
  it.

  `main()` now sets `errors="replace"` on stdout and stderr, and `Log.__call__`
  carries a fallback for a stream that cannot be reconfigured. The echo is lossy;
  `run.log` is opened UTF-8 independently and still holds every character, so
  nothing is lost but a `?` on a console that could not have shown the character
  anyway. `tools/progress.py` got the same treatment - it prints lines lifted
  straight out of `run.log`.

  This could never have been fixed by writing careful ASCII in this repository:
  the console's encoding belongs to whoever launched the run, and worker text is
  arbitrary. Note it is invisible from an interactive PowerShell, which is UTF-8 -
  the self-test case pins `PYTHONIOENCODING=cp1252` deliberately.

### Added

- **A circuit breaker for the usage wall, and a run that waits it out rather
  than burning the plan against it.** When a worker returns nothing at all -
  exits non-zero having produced no result event - the runner now counts it. Three
  of those in a row (`run.wall_threshold`, `--wall-threshold`) means nothing can
  succeed right now, and the run stops treating the plan as the problem.

  The step it happened on is recorded **`NOT RUN`**, never `STUCK`: no worker read
  the code, so nothing about the code was learned, and its note says so. `NOT RUN`
  is resumable, so a later launch picks it straight back up, and it is not a
  blocking outcome, so `--mode` does not report the plan as BLOCKED for it.

  What happens next is `run.on_wall` / `--on-wall`:

  - **`park` (the default)** waits and probes every 30 minutes
    (`run.park_poll_min`, `--park-poll-min`) until the account answers, then
    resumes at the step it was on. The probe is a haiku worker with no tools and a
    one-word answer, so testing "is the account alive" costs a rounding error
    rather than re-spending a build brief. While parked it logs every five minutes -
    a parked run and a hung run must not look alike.
  - **`stop`** ends the run there, leaving every remaining step pending.

  Parked time does **not** extend the stop time: `--hours` is a promise about when
  you can look, not a quantity of compute owed. `SUMMARY.md` says how much of the
  run went to waiting, over how many probes and at what cost, so the per-hour
  figures cannot quietly lie about what the night bought.

  This is the fix for the run of 2026-09-07, where the account's five-hour window
  closed mid-run and the runner - unable to tell "this step failed" from "nothing
  can succeed" - retried, diagnosed and marked `STUCK` thirty-odd untested steps
  in about twenty minutes. The detection is deliberately **not** a search for a
  quota message in the error text: that would be brittle and would miss a
  logged-out CLI, a withdrawn model or a dead network, which fail the same way and
  deserve the same answer.

- `tools/progress.py <project dir> ...` prints **one line per project** and
  nothing else: whether a run is live, the step it is on this minute, how far
  through the plan it is, and what it has spent. It exists for the question asked
  at 3am, where a report that needs reading is a report that does not get read.
  Read-only - it takes the liveness from the run's own `.lock` (so it needs no
  process table and no arguments beyond the project directory), reads `run.log`
  and the plan, and writes nothing, so it is safe against a run in flight, which
  is the only time anybody wants it. A lock whose pid is dead reports as a
  finished run rather than a live one.

## [1.0.1] - 2026-09-06

### Changed

- **No worker of any kind can now reach a tool that acts outside the tree.**
  `UNATTENDED_DENY` denies `Artifact`, `CronCreate`, `CronDelete`, `CronList`,
  `DesignSync`, `PushNotification`, `RemoteTrigger`, `SendMessage` and `Workflow`
  to build, review, reflect and diagnostic workers alike. Each of them publishes
  a page, schedules or fires work that outlives the run, messages somebody, or
  fans out further agents - none of which the git undo underneath this runner can
  reach, with nobody awake to see it happen. The review step's
  `Edit`/`Write`/`NotebookEdit` fence is unchanged and now sits on top of that
  list, in a single `--disallowedTools` rather than two. A tool name the installed
  CLI does not have is inert, so the list can safely carry one only some versions
  ship. Roughly 4% of a run's tokens go with them, which is a bonus and not the
  reason.
- Every worker's brief now also tells it to **keep tool results small** - a
  result is paid for when it arrives and again on every call after it, which is
  the largest component of the 56% of a run's spend that is context re-reads. In
  practice: Grep to locate before Read, `offset`/`limit` on a large file, no
  re-reading what is already in context, filter a noisy command rather than
  dumping it. Nothing is forbidden; it changes what a worker reaches for first.

### Added

- `SUMMARY.md` now **splits the run's cost into building and not-building** -
  review and reflect steps, and the rework they order - with counts of the build
  steps that took more than one attempt or were reworked after a review. The
  efficiency bar is structural (review, reflect, rework and discarded attempts
  were 26% of the first real run), so it is now a number the operator sees every
  morning rather than one a document asserts. The kind split is exact; the
  retried/reworked counts are indicative, because the ledger keeps one cost per
  step and a step's discarded attempts sit inside its own figure.
- Every worker - build, review, reflect, diagnostic - is now told, in its brief,
  to prefer `Read`, `Grep`, `Glob` and an Explore-style subagent (if the Agent
  tool is available to it) over Bash for reading and searching the codebase, and
  to reserve Bash for the test suite, build scripts and git. `cat`/`sed` reads
  cost far more input tokens than the equivalent structured call; this does not
  restrict what a worker *can* do (Bash stays fully available for gates and
  commits), only nudges what it reaches for first.

### Fixed

- **A rework's cost never reached the ledger.** `run_build` charges its workers
  to the step it is building, but a rework rebuilds a step that was recorded long
  before and whose cost was flushed with it, so the spend sat in memory until the
  process exited and was missing from `SUMMARY.md` and from every `--mode` cost
  total. It is now attributed to the review step that ordered it, which is also
  where it belongs when reading the overhead split. The self-test's new guard is
  the invariant itself: the ledger total must equal what the worker logs say was
  spent, whatever path spawned them.
- README section 12's permission table still said reflect and diagnostic workers
  ran under `acceptEdits`. That was changed to `bypassPermissions` before v1.0.0
  was even cut (a reflect must be able to do more than edit and commit; a
  diagnostic must be able to run the failing test it is asked to explain) - the
  doc just never caught up.

## [1.0.0] - 2026-09-06

Initial public release. See `README.md` for the full design; the highlights:

- Four step kinds - build, review, reflect, gate - driven by a steps
  specification, with git as the undo.
- Each build step runs in its own git worktree by default (`run.isolation:
  worktree`), integrated onto the operator's branch only once its gates pass, so
  a file created in the operator's own tree mid-step cannot fail a worker's
  gate. `in-place` is the opt-out.
- A reset quarantines untracked files instead of deleting them, and tags every
  commit it discards as `rescue/<step>/<n>` rather than losing it.
- A reset refuses outright if any commit in range was made by something other
  than this run's own workers.
- The plan file is the ledger: outcomes are spliced into each step's own block
  and committed, so `--mode` alone is a sufficient handoff after a `/clear`.
- `selftest.py` exercises every path against a scripted fake worker in about two
  minutes, for no tokens; passing it is a hard rule before any launch.
- Proven across two real overnight runs the day of release (`docs/`), including
  the defect that made worktree isolation the default and the review-to-rework
  cycle catching two real bugs a build step's own gates had missed.
