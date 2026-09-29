# Continuity gate evaluation

Numbers behind the `blend` continuity detector (default since 2026-09-29) and
any future gate change. CLAUDE.md: change the gate only with numbers from
this set and owner approval.

## Data

- `labeled/` — tuning set: 66 real gate decisions (Brazil run
  `data/runs/20260927T220608Z` + military benchmark 2c).
  - `manifest.json` windows (source path, `k_s`, start/end, legacy verdict);
    `labels_sonnet.json` blind labels (2 fps sheets, Sonnet sub-agent);
    `labels_corrections.json` two corrections found by frame checks
    (final: 50 continuous / 16 transition);
  - `labels_audit.json` + `audit_ids.json` blind 4 fps re-audit of the 35
    items where old and new rules disagreed;
  - `replay_legacy.json` / `replay_blend.json` product-gate decisions.
- `heldout/` — 22 windows from the live horses run (never used for tuning).
  `labels_blind.json` is keyed by blind IDs H00..H21; `audit_ids.json` maps
  them to window ids. `labels_corrections.json` (keyed by window id) holds the
  3 loop-seam jump cuts found frame by frame. The stored replays carry
  `label: "?"` because replay_product.py reads labels_sonnet.json, which the
  held-out set does not have; score it by joining labels_blind.json via
  audit_ids.json and applying the corrections.

Result (product code): tuning set blend keeps 0/16 transitions, rejects 2/50
and trims 2/50 continuous shots (legacy: 0/16, 25/50, 9/50). Held-out: blend
0 errors; legacy kept the 3 jump cuts and trimmed 1 continuous shot. The
held-out set had no dissolves.

## Reproduce

The source media are gitignored analysis copies (data/cache/analysis and
tmp/bench/*/data/cache/analysis); they exist only on this host.

```bash
# rebuild windows + 2 fps sheets (tmp/continuity_eval), then restore labels
.venv/bin/python benchmark/continuity_eval/build_set.py tmp/continuity_eval \
    data/runs/20260927T220608Z tmp/bench/step2c-military-20260928T190619Z/data/runs/seeded
cp benchmark/continuity_eval/labeled/labels_*.json tmp/continuity_eval/
# replay the PRODUCT gate (the number that counts), ~4 min each
.venv/bin/python benchmark/continuity_eval/replay_product.py blend
.venv/bin/python benchmark/continuity_eval/replay_product.py legacy
# held-out set: EVAL_DIR=tmp/continuity_heldout (build_set on the horses run)
```

Tuning helpers (paths HARD-CODED to tmp/continuity_eval): `product_scans.py` caches
product-path scans to `product_scans.pkl`; `tune_product.py` and
`tune_median.py` sweep thresholds on that cache. `dissolve.py`, `signals.py`
and `score.py` are the earlier exploratory signal scripts (their own decode,
not the product path).

## Lessons (keep)

- Always confirm numbers by replaying the product code: the exploratory
  scripts decoded differently and hid an OpenCV 5.0 bug (`phaseCorrelate`
  with a window multiplies it into its inputs in place).
- Blind sheet labels miss short dissolves and loop-seam jump cuts; check
  disagreements frame by frame (ffmpeg, every frame, `-t` before `-i`).
