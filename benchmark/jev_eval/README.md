# Offline Jev evaluation (2026-09-29)

Question: can Jev (`typesafe/jev-1.13`, OpenRouter Decisions API; text in, calibrated
typed answers out, no images) (1) pick better sources from search results than search
order does, and (2) catch vision `keep` labels whose own note reports a problem?
Everything replays existing runs; no YouTube traffic.

## Data and method

- `build_dataset.py` -> `data/sources.json` (184 candidates across 10 briefs: batch1
  R01-R11, step4 red deer, step5 Iceland) and `data/strips.json` (116 vision strip
  notes; 26 are delivered clips with eye verdicts from `benchmark/batch1/judge/`).
- Outcome per analyzed source (44 whose run reached the shortlist): `productive` if
  the vision model kept at least one strip (geo not conflicting) and not every
  delivered clip was judged bad; otherwise `dead`. This label inherits vision errors.
- Blind reference labels (`data/ref_*.json`): one Sonnet (sonnet-high) pass over
  metadata and notes only, with no outcomes and no Jev answers. They are a
  frontier-judge reference, not ground truth.
- `run_jev.py {sources-flat|sources-snippet|sources-full|strips} [--qv v1|v2]`: questions `jev_eval_sources_v1`
  (subject, place, kind, conditions, usable) and `jev_eval_strips_v1` (violation),
  written once and not tuned on this data. `flat` state = title, channel, duration
  and views (roughly a search hit); `full` adds description, tags, chapters and
  categories. Answers are cached in `cache/`. The key is read into the process only.
- `analyze.py [--details]`: AUCs, top-5 replay (rank uses 5 sources per run), and
  strip flag counts.

Cost: 484 calls, $0.022 total (about $0.00006 per call, 1.1-1.5k input tokens);
median latency 0.22 s, p95 0.4 s, 4 concurrent; 0 errors.

## Results

Sources:

| | flat | full |
|---|---|---|
| AUC P(usable) vs reference judge (73 yes / 61 no) | 0.95 | 0.95 |
| AUC P(usable) vs observed outcome (24 / 20) | 0.65 | 0.74 |
| Reference judge's own AUC vs outcome | | 0.745 |
| place choice agrees with reference | 170/184 | 175/184 |

Top-5 replay, summed over 10 briefs (50 picks), using the reference labels:

| picks | ref yes | ref unsure | ref no |
|---|---|---|---|
| search order (today) | 22 | 11 | 17 |
| Jev P(usable), full | 38 | 9 | 3 |
| Jev P(usable), flat | 37 | 9 | 4 |
| best possible by reference | 43 | 7 | 0 |

Search order's 50 picks were 24 productive and 19 dead (7 not observed). Of Jev's
full-state picks, 16 were observed: 12 productive, 4 dead.

Strips (threshold on P(violation)):

| | >= 0.5 | >= 0.7 |
|---|---|---|
| vs reference (36 yes / 80 no); AUC 0.99 | tp 36, fp 5, fn 0 | tp 31, fp 1, fn 5 |
| delivered clips, eye-judged bad (5) | 3 flagged | 2 flagged |
| delivered clips, eye-judged good or acceptable (21) | 3 flagged | 1 flagged |
| vision `keep` strips flagged | 13/86 | 7/86 |
| vision `reject` strips flagged (sanity check) | 16/16 | 16/16 |

## Reading

- Jev reproduces a frontier text judge (AUC 0.95) at negligible cost and latency.
  Where it misses the outcome, the reference judge misses too (0.74 vs 0.745): the
  limit is what metadata reveals, not Jev.
- The main gain is ranking: Jev ordering would have replaced about 14 of 17
  reference-"no" rank slots (black screens, Canadian lakes for "European",
  fallow/spotted deer, explainers). The flat state is nearly as good as full
  against the reference, so ranking search hits before fetching metadata looks viable.
- Two "productive" sources Jev scored low were really off-brief places that vision
  could not tell apart (Alaska aurora for Norway, Australian outback camels for the
  Sahara). The one clear loss is a "Sleep For 11 Hours ... Ocean Sounds" video that
  yielded 2 good clips: sleep videos are mixed, and neither judge can tell from metadata.
- The strip check reads what the vision note says, and nothing else. It caught the
  daytime camels (0.97), but not the pedestrians or the rainy windshield, whose
  notes did not call them defects. Most flags among `keep` strips are real
  note/label contradictions (faint mist, open field instead of forest, watermark, Rockies).

## Question set v2 and the product setting (2026-09-29, later)

`run_jev.py --qv v2` asks `jev_eval_sources_v2`. It was written from the v1 misses, not
fitted: it splits "relaxation film of the real scene" from "dark or still screen" and says
long relaxation films are fine. The `snippet` state adds the first 150 characters of the
description. That is roughly what a flat search hit carries: a live `ytsearch` on
2026-09-29 returned a description snippet of about 120-150 characters per hit. `compare.py`
tabulates every variant and score (all 184 candidates; outcome n = 24 productive / 20 dead):

| variant | score | AUC outcome | AUC reference | top-5 ref yes/unsure/no | observed prod/dead |
|---|---|---|---|---|---|
| search order | | | | 22/11/17 | 24/19 |
| v1 full | usable | 0.739 | 0.952 | 38/9/3 | 12/4 |
| v1 full | mean(usable, subject, conditions) | 0.781 | 0.951 | 36/12/2 | 14/2 |
| v2 snippet | mean(...) | 0.786 | 0.936 | 33/11/6 | 8/4 |
| v2 full | usable | 0.749 | 0.953 | 39/9/2 | 12/5 |
| v2 full | mean(...) | 0.798 | 0.953 | 37/11/2 | 14/2 |

Differences between question sets are within noise (AUC standard error is about 0.07 at
n = 44). Consistent across all variants: combining usable with subject (and conditions)
beats usable alone on the outcome. The product (src/scenery_brief_clips/jev.py, docs/JEV.md)
therefore uses v2 questions (`jev_source_q_v2`, identical wording) and score = mean of
P(usable), P(subject), P(conditions). It orders hits on the snippet state and re-scores
candidates on the full state. Reject threshold 0.35 on the full state: rejects 15 of 184
(13 reference-no, 2 unsure, 0 productive, 2 dead); 0.40 would lose 1 productive source.

## Caveats

Small sample (44 observed sources, 26 eye-judged clips, 10 briefs); the reference is
one model pass; 34 of Jev's 50 top-5 picks were never analyzed, so their real yield is
unknown until a live A/B. The flat state here has no description snippet (real
`--flat-playlist` hits may carry one; not checked because YouTube blocks the host).
