# humanise - render seconds as a short human phrase

*EXAMPLE. Deliberately independent of `parse-durations`, so that if that step is
STUCK this one still runs.*

## Read first

- `src/tickerbell/render.py` - empty but for a module docstring. This is a new
  function, not a rewrite.
- `docs/decisions.md`, "one phrase, two units" - the house rule that a phrase
  never shows more than two units, because "3d 4h 12m 9s" is not human.

## Build

One deliverable: **`humanise(seconds) -> str`.**

- Two units at most, largest first, smaller unit dropped when it is zero:
  `93784` -> `"1d 2h"`, `3600` -> `"1h"`, `59` -> `"59s"`.
- `0` -> `"0s"`, not `""`.
- Negative input raises `ValueError`. Do not invent a "3h ago" form; if you
  think the callers need one, write that to the decisions file.

Do not touch `parse.py`. The two functions are inverses in spirit but not in
code, and coupling them here would put both steps' work in one commit.

## Tests

Add `tests/test_humanise.py`, containing at least:

    test_humanise.py::test_two_units_at_most
    test_humanise.py::test_zero_renders_as_0s
    test_humanise.py::test_negative_raises

## Watch for

- Rounding. `89` seconds is `"1m 29s"`, not `"1m"`. If you find yourself wanting
  a `round=True` argument, that is a second deliverable - decisions file.
