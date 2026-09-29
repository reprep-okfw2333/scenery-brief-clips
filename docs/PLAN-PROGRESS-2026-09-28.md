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

## Status per step (updated 2026-09-29)

1. DONE, committed (ba4779d). Workers default 2; span timeout 90 s + 10 s per
   span second (max 300); `run_deadline_s` (default 10800) -> status
   `deadline`, resumable. Merge to master NOT done (owner: not yet).
2. DONE, committed (ba4779d). Cache policy `v4-copyts-720`: stream copy +
   copyts, one yt-dlp call per video (`YtDlp.prefetch_analysis`), export
   rendition pinned when <=720p (`export.analysis_format_id`), local 0 = first
   packet PTS (marker `first_pts_ms`, range `mapping_k_s`), verify proves it;
   export adopts matching analysis copy (`_adoptable_analysis_copy`,
   `fetch_export(source=)`, plan `acq_source`); continuity sequential sampling
   (`SCENERY_CONTINUITY_SEEK_EACH=1` = legacy); proven cache hits skip
   re-decode. Analyze: ocean 525 -> 116 s, military 980 -> 245 s; yield
   unchanged.
3. DONE, in the "feat: blend continuity detector" commit, redirected by data: keep gate dropped, wider windows not
   needed; the yield loss was continuity-gate false rejects. New `blend`
   continuity detector, the DEFAULT since 2026-09-29 (owner approved).
   Sections "Step 3 ..." below.
4. DONE, in the "feat: blend continuity detector" commit (2026-09-29): planner prompt v2, one retry, `run-pipeline
   --live-planner`. Section "2026-09-29" below.
5. TODO: loosen brief form (duration band e.g. 2-15 s; fewer provenance fields).
6. TODO: skill = one command (`run-pipeline --live-planner --live-vision` with
   agreements up front); fix the skill's origin/master check.
7. TODO (later): real quality signal on review frames (blur/sharpness,
   text/watermark, wrong species / rendered imagery).

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
- Session stopped here on owner request (2026-09-28). At that point
  everything (steps 1-2) was committed on fix/analysis-download-bounds
  (ba4779d); later step 3-4 work was committed in the "feat: blend continuity detector" commit (not pushed).

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

### Step 3 continued (2026-09-28, evening session)

- signals.py v2 run (3m41s): the 1-s-lag masked residual does NOT separate
  dissolves from motion (continuous items reach lag spike 20; missed
  transitions sit at 1.6-3). Dropped.
- New blend-fit test (benchmark/continuity_eval/dissolve.py): a dissolve frame
  b is well explained by alpha*a + (1-alpha)*c of frames LAG before/after, while
  far from both; score = min(|b-a|,|b-c|) / (blend residual + 1), static
  overlay pixels masked, alpha in [0.15, 0.85], ends (compensated a-vs-c) >= 10.
  Decoded with 1 s pad either side (`dissolve.py EVAL_DIR 6 160 1`, ~5 min;
  frames cached in tmp/continuity_eval/frames_6fps_160_pad1.npz) so edge
  dissolves have neighbours; only events inside the window count (edge=0;
  counting pad frames adds 3 false flags).
- Label fixes (benchmark/continuity_eval/labeled/labels_corrections.json):
  YpI_w1KIWOs@5460.810 is a dissolve (Opus, from the blend score, sheet thumb 8
  double exposure); BAx_9MNPzts@2605.349 starts mid-dissolve (found by a blind
  4 fps re-audit of the 35 items where old/new rules disagree, sonnet-high
  sub-agent; confirmed). The audit found no other hidden edits.
  Set is now 50 continuous / 16 transition.
- Candidate = cut rule (compensated residual >=15 and >=3.5x window median, or
  mean-colour jump >=35) OR blend rule (lag 2 or 3 frames at 6 fps, score
  >=1.35). Result: 0/16 transitions missed, 3/50 continuous flagged (two copies
  of one whip pan, one riot-police march). Current gate: 0/16 missed, 34/50
  flagged. 252 of 3240 grid settings reach 0 missed / <=3 false flags.
  Margin is thin: weakest dissolve scores 1.42, strongest continuous 1.24.
  5 of 16 transitions (all dissolves) are caught only by the blend test.
- NOT validated on held-out footage yet (no local runs with fresh edited
  footage). NO product change. Waiting for owner approval to (a) implement
  behind a setting with legacy kept, (b) run a fresh live theme for held-out.

### Step 3: blend detector implemented, opt-in (2026-09-28, owner approved "build it, off by default")

- Owner approved: implement behind a setting with legacy as default; one live
  held-out run on a new theme (no export).
- Code: src/scenery_brief_clips/continuity_blend.py (new) + continuity.py
  dispatch (`continuity_detector: legacy|blend`, config key, runner analyze
  binding only when non-legacy so existing runs keep their hash; manifest key
  only when non-legacy; decisions gain `detector`/`events` only for blend).
  Legacy reject/trim tail factored into `_decision_for_span`; replaying legacy on
  the 66 eval windows reproduces all 66 recorded decisions.
- Bugs found while replaying through product code:
  1. OpenCV 5.0 `phaseCorrelate(a, b, window)` multiplies the window into a and
     b IN PLACE. Reusing frames across pairs corrupted residuals (5 transitions
     missed). Fixed by passing copies; regression test. The eval scripts
     re-derived grayscale per call, so their numbers were unaffected.
  2. Smooth slow motion (zoom/push-in) makes b ~ (a+c)/2 everywhere; with the
     product's time-anchored grid two continuous items were rejected. Added
     BLEND_SPIKE: score must also be >= 1.15x the scan's median score.
  3. A frozen stretch in the scan (still bars) dragged the cut median down so
     ordinary motion passed the 3.5x spike (late-cut fixture, found by the
     sonnet-high test writer). Medians now skip frozen pairs (< 0.5 luma diff).
  4. A fast dissolve (~5 source frames) blended the last kept sample of a trim
     (emw829N580Q@183.759, checked frame by frame). A cut now excludes both
     samples around it.
- Final replay through product code (tmp/continuity_eval/replay_product.py):
  transitions 0/16 kept (10 reject, 6 trim; all 6 trims exclude the labeled
  transition), continuous 2/50 rejected (one whip pan, same window in two runs)
  and 2/50 trimmed; legacy: 25/50 rejected, 9/50 trimmed. Gate time on the 66
  windows (450 s of footage): legacy 205 s, blend 245-265 s.
- Known limitation (xfail test): a synthetic fast zoom (1.0x->1.4x in 6 s)
  yields dissolve events; on real footage such false alarms only trimmed.
- Tests: tests/test_continuity_blend.py (31, written by a sonnet-high sub-agent
  to spec, reviewed). Full suite 436 passed, 1 xfailed.
- bench.sh: BENCH_DETECTOR=blend|legacy overrides the config key;
  BENCH_NO_EXPORT=1 stops before export. Held-out inputs:
  benchmark/heldout-horses/ (horses grazing, 6 videos, 90 s cap, blend).

### Step 3 benchmarks + held-out (2026-09-28, late)

- Seeded benchmarks, same code, legacy vs blend (benchmark/RESULTS-2026-09-28.md):
  military excerpts 3 -> 7, rejects 7 -> 3, 2/2 delivered both, verify ok;
  ocean identical excerpts. Wall: military 4:57 vs 5:47, ocean 4:10 vs 5:04
  (gate +13-16 worker-s, more excerpts to review; export noise on ocean).
- Delivered blend clip YpI_w1KIWOs_e0_2737940-2742273 (trimmed after a
  dissolve) checked frame by frame: one continuous shot.
- Held-out live run (benchmark/heldout-horses, BENCH_NO_EXPORT=1, paused at
  agree_export as intended): 4 sources, 23 ranges, 22 gate windows, shortlist
  3/3. Blind 4 fps labels (sonnet-high) said all continuous; three windows of
  U4qjIADZArM (an 18.6 s loop) contain a hard jump cut at the loop seam
  (checked frame by frame; labels corrected in
  benchmark/continuity_eval/heldout/labels_corrections.json). Result: blend
  rejected exactly those 3 and kept the other 19; legacy kept the 3 jump cuts
  and trimmed 1 continuous shot.
- Caveat: the held-out set had no dissolves, so the dissolve thresholds (thin
  margins) are validated only on the tuning set.
- NEXT: owner decides whether blend becomes the default. Then plan step 4.

### 2026-09-29: blend is the default; step 4 (planner) implemented

- Owner: "Make blend default. Head on to step 4." `DEFAULT_CONTINUITY_DETECTOR =
  "blend"` (ContinuitySettings, config reader, runner binding). Runner binding
  now follows the EFFECTIVE detector: no key = blend, so runs analyzed before
  the flip are re-analyzed on resume unless the config pins
  `continuity_detector: legacy` (which keeps the old binding). Legacy tests pin
  legacy explicitly. Docs: AGENTS.md, config.example.yaml, ARCHITECTURE, CLI,
  STATUS, ISSUES. Full suite 436 passed, 1 xfailed.
- Step 4, measured first (benchmark/planner_eval/, live z-ai/glm-5.3-flash,
  9 briefs x 3 incl. irregular plurals goose/wolf, multiword "Brazilian
  military", "red deer"): prompt v1 first-attempt valid 8/27 (all failures
  subject_mismatch: "horse" for "horses", "geese", "wolves", "Brazilian armed
  forces"). Prompt v2 (states the validator rules: noun exactly as written,
  plain-s plural only, length/charset, no duplicates; "synonym" varies words
  around the noun) 25/27 first attempt.
- Retry: plan_queries retries ONCE when the reply is not JSON or fails
  validation, appending the rejection code/message and the previous reply;
  call errors are not retried. Provenance: model_calls, rejected_attempts.
  Production path (v2 + retry): 27/27 plans, 31 calls. The retry once produced
  "gooses"; the prompt now says to keep the noun as written for irregular
  plurals and never invent one: goose/wolf 8/8 plans, 12 calls, no invented
  plurals. (Accepting irregular plurals in the validator would save the retry;
  not done: it is a validator policy change.)
- Wiring: `run-pipeline --live-planner [--planner-config]` (no --plan) wires
  planner.yaml; Ports.planner_wire; runner no longer crashes on a live plan
  (plan_queries used to dereference wire=None); planner failure fails the
  discover stage with the error; model_calls counted. bench.sh
  BENCH_LIVE_PLANNER=1; plan.json optional in inputs.
- Tests: tests/test_planner_retry.py (29, sonnet-high to spec, spot-checked).
  Full suite 465 passed, 1 xfailed.
