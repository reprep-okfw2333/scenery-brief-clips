# Handoff plan: working log (started 2026-09-28)

This is the running record for working through the "Recommended plan" in
docs/HANDOFF-2026-09-28.md on branch `fix/analysis-download-bounds`. Anyone
(or any agent after a context reset) picks up here. Keep it current after
every step.

## Ground rules from the owner

- One plan step at a time. After each: full test suite, rerun the benchmark,
  compare timings and yield, summarize to the owner, then continue.
- Do not pad results or fake a shortfall away.
- Host: 1 CPU, 1.6 GB RAM. Keep parallel downloads low (2 workers).
- Do NOT commit, merge or push without asking (owner said "not yet" after step 1).
- Owner decisions (2026-09-28): analysis and export may share one download if the
  gates hold (done in step 2); GLM 5.3 Flash on OpenRouter
  (`openai-api` / `z-ai/glm-5.3-flash`) is the canonical vision/planner wire and is
  approved for benchmark labeling; add a harder benchmark (done: military).
- Key: `OPENROUTER_API_KEY`, lives in `~/.hermes/.env`. bench.sh loads it into the
  benchmark process only. Never print or write it.
- Owner may run a Hermes agent on this host; its headless Chrome contaminated the
  step-2c timings. Owner closed Hermes and allowed killing leftovers. Leave the
  long-lived `hermes gateway run` and PID-1599 hermes services alone. Before a
  benchmark check `uptime` / `ps` for other load.

## Benchmark harness

- `benchmark/bench.sh <label> [seed_run_dir]` with `BENCH_INPUTS=improvement`
  (ocean, default) or `BENCH_INPUTS=military`. Fresh root under `tmp/bench/`
  (cold caches). Writes `benchmark/runs/<label>-<utc>/summary.json` etc.
- Seeded runs reuse discover..apply_scores and the metadata cache from:
  - ocean: `tmp/bench/baseline-20260928T154614Z/data/runs/20260928T154616Z`
  - military: `tmp/bench/military-base-20260928T165504Z/data/runs/<only dir>`
  Do not delete those two baseline roots.
- Run one benchmark at a time (1 CPU). Never run pytest concurrently with a benchmark.
- All modules the runner uses are imported at process start (planner/jev lazily),
  so editing src/ after a benchmark has started does not leak into it.
- Results table: `benchmark/RESULTS-2026-09-28.md` (append per step).
- Noise: analyze on unchanged code varied 451-525 s (ocean). Treat <15% as noise.

## Status per step

1. DONE (uncommitted). Workers default 2; span timeout 90 s + 10 s per span
   second (max 300); `run_deadline_s` (default 10800) -> status `deadline`,
   resumable. Merge to master NOT done (owner: not yet).
2. DONE (uncommitted). Cache policy `v4-copyts-720`: stream copy + copyts, one
   yt-dlp call per video (`YtDlp.prefetch_analysis`), export rendition pinned
   when <=720p (`export.analysis_format_id`), local 0 = first packet PTS
   (marker `first_pts_ms`, range `mapping_k_s`), verify proves it; export adopts
   matching analysis copy (`_adoptable_analysis_copy`, `fetch_export(source=)`,
   plan `acq_source`); continuity sequential sampling
   (`SCENERY_CONTINUITY_SEEK_EACH=1` = legacy); proven cache hits skip re-decode.
   Analyze: ocean 525 -> 116 s, military 980 -> 245 s; yield unchanged.
   Tests 403 passed.
3. IN PROGRESS, redirected by data (see step notes and HANDOFF.md): keep gate
   dropped, wider windows not needed; the yield loss is continuity-gate false
   rejects. Stopped after scoring candidates on the labeled set; signals.py v2
   (lagged, overlay-masked residual) written but NEVER RUN. No product change.
4. TODO: planner prompt states exact-subject rule; retry once with validator
   error; wire live planner into run-pipeline.
5. TODO: loosen brief form (duration band e.g. 2-15 s; fewer provenance fields).
6. TODO: skill = one command (`run-pipeline --live-vision` with agreements up front).
7. TODO (later): real quality signal on review frames (blur/sharpness, text/watermark).

## Open questions / pending decisions

- RESOLVED: export encode preset. Owner asked me to pick; measured on two real
  clips (idle CPU, crf17, 1 thread): medium 61.6/31.2 s, faster 33.3/23.0 s
  (+3% size, -0.06/-0.12 dB PSNR), veryfast 28.4/14.2 s (+7-16%, -0.14/-0.39 dB).
  Picked `faster`: EXPORT_RECIPE x264-crf17-faster-v1; verify ACCEPTED_RECIPES
  keeps x264-crf17-medium-v1 so published exports still verify.
- Shortlist diversity does not prefer distinct sources (ocean 2a picked two
  moments from one video). Possible quality item, not yet raised as a change.

## Step notes

- Compatibility fix found 2026-09-28 after step 2: verify rejected every run
  analyzed before v4 (Brazil run: 256 errors). Now verify checks each run under
  the cache policy its manifest recorded (ACCEPTED_ANALYSIS_POLICIES = v4, v3).
  Brazil run `data/runs/20260927T220608Z` verifies ok again with
  --require-export (16 acquisitions checked). Tests 405 passed.
- Host cleanup 2026-09-28: killed orphaned headless Chrome trees and ten orphaned
  browser_harness daemons (owner allowed). Hermes gateway services left running.

- Corrected labels (tmp/continuity_eval/labels_sonnet.json, copy in
  benchmark/continuity_eval/labeled/): 52 continuous, 14 transition. Current gate:
  0/14 transitions missed (rejected or trimmed) but 36/52 continuous flagged
  (26 rejected). Best simple candidate (6 fps, compensated residual >=15 and
  >=3.5x window median): 3/14 missed (all cross-dissolves: 2 news broadcast
  with static overlays, 1 stock montage), 3/52 flagged.
- Session stopped here on owner request (2026-09-28). Everything committed on
  fix/analysis-download-bounds; see HANDOFF.md for next steps.

(Append findings here as each step progresses.)

### Step 3 findings so far (2026-09-28)

- Offline check on the Brazil run (18 sources): keep count / keep ratio does NOT
  predict yield. YSEyA80dxKo 7/8 keeps -> 0 excerpts; Ynp0PjMC8uo and
  JtAXrYugk_Y at 0.25 ratio supplied 3 of the 16 delivered clips. A ratio>=0.5
  gate would have lost 3 delivered clips; ">=2 keeps" would drop only
  iZyDYIEKxoU (1 keep, 0 delivered). Not implementing the handoff's gate as written.
- Military benchmark: PySceneDetect finds NO cut in any of the 10 spans (each
  one shot), yet the continuity gate rejected 7/10 ("stable subspan <4 s").
  Contact sheets (tmp/exp/sheet-a.jpg, sheet-b.jpg) show continuous handheld
  pans of marching troops: false rejects caused by camera motion. Wider
  windows would not help (no cuts inside spans).
- Plan: labeled eval set of real gate decisions (benchmark/continuity_eval/
  build_set.py -> tmp/continuity_eval/: 66 items = 37 reject, 13 trim, 16 keep
  from the Brazil run + military 2c), blind labels by a Sonnet sub-agent
  (tmp/continuity_eval/labels_sonnet.json), spot-checked by me; then measure
  current gate vs a motion-tolerant candidate; show numbers to owner before
  changing the default (docs say do not weaken gates).
- First labeling pass (Sonnet, blind) was on BUGGY sheets: ffmpeg `-t` was an
  output option and `tile` emits one frame at the end, so sheets for windows
  <12 s showed footage past the window. Old sheets/labels kept in
  tmp/continuity_eval_v1/ (do not use). Fixed build_set.py (input-side -t),
  rebuilt sheets, relabeling blind (labels_sonnet.json). "continuous" labels
  from v1 were still valid (superset); v1 said 24/37 rejects continuous.
- Signals (benchmark/continuity_eval/signals.py -> tmp/continuity_eval/
  signals_6fps.json) were window-limited correctly. Early read: motion-
  compensated residual "spike" (max/median) and max mean-colour jump separate
  cuts from pans better than the gate's dHash+endpoint thresholds; pans in
  marching footage have max residual 25-52 but spike ~1.4-2.5.
