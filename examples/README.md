# examples - a complete run package, for a project that does not exist

Everything here is **fictional**. `tickerbell` is an invented library that turns
durations into words. No file, path, repository or person referred to in these
briefs is real. The package is here for two reasons: to show the shape of a plan
that works, and to let a new project watch the loop run before anybody writes a
real brief.

## Run it

    python examples/try_it.py

It builds a throwaway git repository, copies this package into it at the
convention's location, and runs the real runner against `fake_worker.py` instead
of `claude`. It takes about a minute, spends nothing, and touches nothing outside
the scratch directory. `--keep` leaves the repository behind to poke at.

You will see, in order: a build step pass and commit; a review return **rework**
and a second attempt land on top of the reviewed commit; a build fail its gate,
reset, and pass on the retry; a reflect decide the plan needs no change; a last
build and a review that passes. Then the digest and `SUMMARY.md`.

Needs `git` and `pytest`.

## What is here

| file | what it demonstrates |
|---|---|
| `steps.example.yaml` | the plan: universal gates, per-step gates, tiers, where reviews and reflects go, `expected_min` against `timeout_min` |
| `briefs/_preamble.md` | the rules every worker gets: commit by explicit path, write findings as you learn them, what never to do |
| `briefs/parse-durations.md` | a build brief: read first, one deliverable, named test node ids, what NOT to touch |
| `briefs/humanise.md` | a second brief, deliberately independent of the first |
| `briefs/cli.md` | a brief that depends on the two before it, which is why it sits after the reflect |
| `briefs/_reflect.md` | project-specific guidance for a reflect step |
| `try_it.py` | the scratch repository and the fake run |

## Using it for a real project

Copy `steps.example.yaml` to `<your project>/overnight/steps.yaml` and the briefs
to `<your project>/overnight/briefs/`, then replace the contents. Keep the shape:

- one deliverable per build step, about fifteen minutes;
- every behavioural exit criterion is a **named test node id**, identical in the
  brief and in the gate;
- every brief says what the step must **not** do;
- a review after any step that sets a shape later steps build on;
- a reflect after every block of three to five.

`/overnight plan` will do all of this with you against your actual project, which
is the better route. This directory is what it copies from.
