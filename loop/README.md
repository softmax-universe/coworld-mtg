# Local improvement loop

One command runs a whole improvement epoch on one machine, with no scheduler:

```bash
python3 loop/run.py epoch --run-dir tmp/loop-runs/e1
```

An epoch does this, in order:

1. **Snapshot** the source tree into `<run>/baseline`, then build it and run
   the checks (format, lint, tests).
2. **Fit** every game in the cohort with the baseline fitter.
3. **Discover**: group what went wrong by cause (`discovery.json`).
4. **Brief** a contributor: `<run>/candidate/BRIEF.md` lists outcomes, grouped
   issues, the fitter's own rejection counts per game, the assumed libraries,
   and the oracle text of the cards involved.
5. **Contribute**: a headless `claude -p` agent edits the candidate tree. It can
   run `./try <game...>` to rebuild, check and refit single games, and it writes
   `PROPOSAL.json` saying what it changed and why.
6. **Check scope**: the diff against the baseline is saved as `candidate.patch`.
   Any edit outside `target.allowed_paths` rejects the epoch.
7. **Re-measure** from a clean copy (`<run>/final`): build, check, refit.
8. **Gate** (`gate.py`): no game may regress, and at least
   `minimum_improved_cases` must improve.
9. **Review**: a second model with no tools reads the patch and the comparison
   and can veto. If the model call fails, the next model in
   `reviewer.fallback_models` is tried.
10. **Decide**: `decision.json` is `accepted`, `rejected`, `no_change`, or
    `inconclusive`. With `--apply`, an accepted patch is copied into the
    working tree. Nothing is committed.

`python3 loop/run.py loop --epochs N --run-root DIR` chains epochs, applying each
accepted patch before the next one starts, and stops at the first epoch that
keeps nothing.

If an epoch passes the gate but the review could not be completed, redo only
the review with `python3 loop/run.py review --run-dir <run> [--apply]`.

## What counts as better

A game improves when it newly matches, matches more milestones, fully covers
more milestones, or checks more recorded fields while still matching. A game
regresses when it loses a match, matches fewer milestones, stops finishing,
or drops or rewrites a field it used to enforce. Using fewer search nodes is
not an improvement. The rules live in `gate.py`; a candidate cannot edit them.

## Settings

`policy.json` holds the cohort, search bounds, the paths a candidate may edit,
the build command and checks, and the contributor and reviewer settings.

- `--card-data PATH` overrides the card data file. Public MTGJSON-derived data
  works when the private corpus is not available.
- `--contributor manual` stops after the brief so a person can edit
  `<run>/candidate` and rerun; `--contributor none` measures only.
- `COWORLD_LOOP_EXEC` is a command prefix for builds, tests, and fits. Use it
  to run them in a container when the host has no Rust toolchain.

## Recorded runs

[`runs/2026-11-24`](runs/2026-11-24/README.md): five accepted epochs. Four took
the playable games from 7 of 11 matching to 11 of 11 at three turns each; the
fifth started on four turns.

## Limits

- The fitter measures agreement with recorded games under stated assumptions
  (see each game's `constraints.json`). It is not a rules-correctness proof.
- A candidate cannot change the Phase engine, which is pinned in `Cargo.toml`.
  Engine bugs the loop finds have to be fixed upstream.
- Games with mulligans are skipped as input issues.
- Run directories are scratch output. Keep them outside version control.

## Tests

```bash
python3 -m unittest discover -s loop/tests
```
