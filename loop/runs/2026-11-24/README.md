# Local loop run, 2026-11-24

Five epochs in a row, each with a model contributor and an independent model
reviewer. All five were accepted. The cohort is the
[frozen 20 games](../../../fixtures/17lands/sos-cohort-20/README.md), fitted at
three turns per player (epoch 5: four turns) with 10,000 search nodes per game.
Nine games have mulligans and are skipped as input issues throughout.

| Epoch | Games matching, before → after | What the contributor found and changed |
| --- | --- | --- |
| [1](epoch-1/) | 7 → 8 | Phase offers only one sample answer to a library search, so a fetched land could never be the recorded one. The fitter now tries every answer the pending search lists. |
| [2](epoch-2/) | 8 → 9 | Backing out of a cast changed an AI-only field, so the repeated-state check never fired and the search looped on cast and cancel. That field is now left out of the state identity. |
| [3](epoch-3/) | 9 → 10 | An in-game shuffle threw away the library order the reconstruction had assumed. A shuffle now keeps the assumed order of the cards that remain. |
| [4](epoch-4/) | 10 → 11 | A branch that had already cast or drawn more than the recording allows was only rejected at end of turn. It is now rejected as soon as it exceeds the record. |
| [5](epoch-5/), four turns | 3 → 3, one game two milestones further | A turn ending with unspent mana or damaged creatures was refused without being compared. Those two cleanup events now count as neutral at the turn boundary. |

Each epoch folder holds the grouped issues the contributor was shown
(`discovery.json`), its patch (`candidate.patch`), the per-game comparison the
gate used (`comparison.md`), and the decision with the proposal and review
(`decision.json`). Contributors took 9 to 22 minutes and about $2 to $3.50 of
model usage each.

## What this does and does not show

- These are matches on supported fields under stated reconstruction
  assumptions, on games that had been inspected before. They are not proof of
  rules correctness and not results on unseen games.
- The runs used card data built from public MTGJSON data, not the private
  corpus.
- The first two epochs needed the review step rerun by hand after a model call
  timed out or returned a malformed reply. The runner now retries with another
  model, and epochs 3, 4 and 5 ran unattended.
- After epoch 4 the state identity was made independent of hash-map order, so
  node counts no longer vary between identical runs. Counts in the comparison
  tables of epochs 1 to 4 predate that and can differ by a few percent.
- At four turns per player 3 of the 11 playable games match, and the other 8
  run out of search budget. `policy.json` now targets four turns.
