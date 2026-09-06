# cli - one command over the two functions

*EXAMPLE. This step depends on the two before it, which is why it sits after a
reflect: if either is STUCK, the reflect can cut this step rather than let it
fail on ground it cannot fix.*

## Read first

- `src/tickerbell/cli.py` - a stub `main()` that prints the version.
- The two functions built earlier in this run: `parse_duration` and `humanise`.
  Read what was actually committed, not what the briefs asked for; they may
  differ, and what is on disk wins.

## Build

One deliverable: **`tickerbell <duration>` prints the humanised form and exits 0;
bad input prints the error to stderr and exits 2.**

- Argument parsing with `argparse`, one positional. No subcommands, no config
  file, no colour.
- The entry point is a thin wrapper: it wires the two library functions together
  and does the I/O, and contains no duration logic of its own. If you find
  yourself writing an `if unit ==` here, it belongs in the library.

Do not add a console-script entry point to packaging metadata - that is a
packaging change and this run does not touch packaging.

## Tests

Add `tests/test_cli.py`, containing at least:

    test_cli.py::test_prints_humanised_form_and_exits_zero
    test_cli.py::test_bad_input_exits_two_and_says_why

Drive `main()` directly with an argument list; do not spawn a subprocess.

## Watch for

- Exit codes. `argparse` exits 2 on its own errors, which happens to match, but
  make the bad-duration path explicit rather than relying on that coincidence.
