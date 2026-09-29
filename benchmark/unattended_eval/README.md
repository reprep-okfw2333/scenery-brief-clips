# Unattended-first track: offline evaluations (2026-09-29)

Run from the repo root with `.venv/bin/python`. Inputs are the local run
directories under `tmp/bench/` and `data/runs/` (gitignored, this host only).

- `yield_study.py`: per run: sources, moments, match/geo labels, exclusion
  reasons, surplus keeps. Finding: the geo gate never excluded a keep; place
  cost clips through `match` (R09 "European alpine lake").
- `label_budget.py`: first estimate of the strip label budget from stored
  labels (270 -> 170-204 labels).
- `replay_budget.py WORKDIR`: replays the PRODUCT labeler
  (`label_review_strips(budget=True)`) with a fake caller returning the stored
  labels, on copies. Result: 270 -> 185 labels, selected 93 -> 94, no run
  lost a clip.
- `relabel_place.py OUT.json [--baseline]`: relabels 57 stored non-keep strips
  and 13 keep controls live with the current strip prompt (needs
  OPENROUTER_API_KEY in the process env; ~70 GLM calls, no YouTube).
  `--baseline` blanks PLACE_SOFT_RULE (the old prompt) to measure model noise.

Place rule results (non-keep -> keep; controls kept):

| prompt | flips | controls | note |
|---|---|---|---|
| old prompt (baseline, noise) | 5 | - | 4 are the Brazil run, first labeled by another model |
| v2 place rule | 28 | 13/13 | also kept a strip whose note says it cuts, and an R05 caption clip |
| v2b (+ "relaxes only place and setting") | 24 | 13/13 | shipped |

v2b vs baseline: 18 alpine lakes doubted only for place (R09 9, Jev control 6,
Jev arm 3) and 3 of 4 R07 "meadow, not forest" deer become keeps. The Moraine
Lake strip (recognizably Canada) stays conflicting. Title overlays, "no lake
in any frame", a woman talking to camera, galloping instead of grazing and
Kirkjufell without a waterfall stay rejected. Files: place_relabel_*.json.
