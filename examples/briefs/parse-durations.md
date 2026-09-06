# parse-durations - turn a duration string into seconds

*EXAMPLE. `tickerbell` does not exist. This shows what a brief looks like: read
first, one deliverable, a named test, and an explicit list of what not to touch.*

## Read first

- `src/tickerbell/parse.py` - today it handles `"90s"` and `"5m"` only, by two
  hand-written `if` branches, and returns `None` on anything else. The `None` is
  the problem: three callers already treat it as zero.
- `tests/test_parse.py` - the existing tests, all of which must keep passing.
- `docs/decisions.md`, the section "why durations are strings" - it explains why
  this is not just `int(seconds)`, and the reason still holds.

## Build

One deliverable: **`parse_duration(text) -> int` accepts a compound duration and
raises on anything it does not understand.**

- Accept a sequence of `<number><unit>` parts with optional spaces: `"1h30m"`,
  `"1h 30m"`, `"2d4h15s"`. Units are `d`, `h`, `m`, `s`.
- Return whole seconds as an `int`.
- Raise `ValueError` with the offending text on: an empty string, an unknown
  unit, a number with no unit, a unit with no number, or a negative value.

Do **not** change the return type of anything else in `parse.py`, and do not
touch the three callers - a later step covers those, and a change here that
half-migrates them makes that step's diff unreadable.

## Tests

Add `tests/test_parse_durations.py`. The gate runs the whole file, and these
node ids must exist in it:

    test_parse_durations.py::test_compound_units_sum_to_seconds
    test_parse_durations.py::test_unknown_unit_raises_with_the_text
    test_parse_durations.py::test_empty_string_raises

Show `test_unknown_unit_raises_with_the_text` failing against the current
`return None` before you change it.

## Watch for

- The old two-branch code is used at import time in `src/tickerbell/cli.py`. If
  making it raise breaks the import, that is a real finding - write it to the
  decisions file rather than adding a `try/except` to paper over it.
- If you find that a caller relies on `None` meaning "no limit", stop. That is a
  semantic decision, not a parsing one, and it belongs to the morning.
