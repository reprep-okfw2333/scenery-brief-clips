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
5. DONE, committed and pushed (2026-09-29): brief form loosened per owner
   decisions; live 1080p/Iceland run completed 5/5, verify ok; standalone
   export/analyze now apply the brief's cap. Section "Step 5" below.
6. ON HOLD (owner, 2026-09-29: no Hermes/part-6 work yet): skill = one
   command (`run-pipeline --live-planner --live-vision` with agreements up
   front); fix the skill's origin/master check.
7. DEPRIORITIZED (owner, 2026-09-29): quality signals on review frames
   (blur/sharpness, text/watermark, wrong species / rendered imagery).

### Priorities and new order (owner, 2026-09-29)

Goal: unattended runs from any request (generic to specific) to clips, with
an AI orchestrator overseeing. Priority now: fast, bug-free, cheap,
consistent runs. Accepted: sacrificing specific-request enforcement
(setting/elements/exclusions) and watermark detection, and some imperfect
clips if most are good. Owner idea: brief/vision profiles by request
specificity, with the orchestrator offering options and asking questions
before writing the formal brief. Work order (approved "yes"):

- B1. Batch baseline: 8 varied, mostly generic requests end to end
  (benchmark/batch1/, benchmark/batch.sh, benchmark/batch_summary.py):
  unattended completion, clips delivered vs requested, clips good by eye,
  wall time, model calls. DONE: 6/8 unattended, 26/36 clips, 21/26
  usable by eye; results benchmark/RESULTS-2026-09-29-batch1.md.
- B2. Speed/reliability fixes the baseline points to. Candidates: the two
  verify passes (215 s of 1458 s on Iceland), search sleeps, review-frame
  extraction, auto-redo of an interrupted stage whose outputs are derived
  (today it needs file cleanup + --acknowledge-uncertain), and
  max_analyze_videos defaulting to 1 (an unattended run without a config
  analyzes one video whatever n_clips is).
- B3. Brief profiles (quick default, standard; specific later) with the
  contract doc telling the orchestrator when to offer options and what to
  ask. Measure with benchmark/brief_eval + the batch.
- Later: the specific profile and quality signals.

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
  (ba4779d); later step 3-4 work was committed in the "feat: blend continuity detector" commit (pushed 2026-09-29 with step 5).

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

### Step 5: brief form (2026-09-29; validated and committed later that day)

Owner decisions: accept my recommendations (loosen duration band, export
height, provenance defaults; keep 1280x720 floor, 16:9 aspect band, search
caps, no-download) and ALSO loosen geography, amount left to me. I chose
"free-text place, soft check" (below). The owner then asked to stop and hand
off; work stopped mid-validation.

Done (working tree, not committed):
- brief.validate_brief: clip_duration_s any band 2 <= min <= target <= max
  <= 30 with min < max; export_max_height 720 or 1080; geography null,
  "european" or a place name (letters, spaces . , ' - ; <= 60 chars);
  `sources` needs only scene.subjects and n_clips (omitted keys =
  project_default; origin/quote rules unchanged). Constants DURATION_FLOOR_S,
  DURATION_CEILING_S, EXPORT_HEIGHTS, REQUIRED_SOURCES, _GEOGRAPHY_RE.
- Found: the brief's export_max_height was never used downstream (export
  read config only). runner._config_with_brief_export_cap now makes the
  brief's cap effective, lowered by config if config sets less; config is
  left untouched when nothing changes, so old runs keep their export binding.
  Follow-up (2026-09-29, later session): run-brief and the runner record the
  brief's cap in discovery.json (`export_max_height`); the standalone
  `export` and `analyze` commands apply it with the same rule
  (export.config_with_requested_export_cap, shared with the runner). Runs
  without the key (legacy `run`, older runs) keep the config-only cap; an
  invalid recorded value exits 2. tests/test_export_cap_request.py.
- Found: geography "european" was never enforced (Constraint.geo_requirement
  had no reader). Geography now flows to constraint.json geo_requirement; for
  a free-text place, vision_wire._theme_from_run appends "Requested place: X.
  Set geo to conflicting only when the frames clearly show a different kind of
  place ..." so shortlist excludes clear contradictions (existing geo
  "conflicting" rule) and keeps unrecognizable scenery flagged geo_uncertain.
  "european" and null behave exactly as before.
- docs/SEARCH_BRIEF.md: header updated (implemented), schema section rewritten
  as the current contract with mapping rules for durations, 1080p/4K/vertical,
  places, style words, exact quotes; example validated by tests.
- Found and fixed: tests/test_brief.py classes were named `*Tests`, so pytest
  never collected its 29 tests (all earlier suite counts excluded them).
  Renamed to `Test*`; 2 tests that pinned the old rules were rewritten.
- New tests/test_brief_step5.py (112, sonnet-high to spec, spot-checked).

Measured (benchmark/brief_eval/): a sonnet-high agent playing the operator
wrote briefs for 12 plain requests from the contract doc only.
- Before (old doc + old validator): 10 briefs written, 2/10 valid; places
  (Iceland, Norway, Tokyo, Sahara, Kyoto), 1080p and non-default durations were
  not expressible; asked correctly on R06 (no count) and R12 (vertical).
- After (new doc + new validator): 9 briefs written, 9/9 valid, every
  requested place/duration/resolution expressed; asked correctly on R06 (no
  count), R08 (4K) and R12 (vertical). Remaining soft spots are by design
  (style words only in theme_text; exclusions recorded, not enforced).

Live validation (DONE 2026-09-29, later session): agent-written brief R02
"5 clips of waterfalls in Iceland, 1080p" (benchmark/step5-iceland,
BENCH_LIVE_PLANNER=1). The stopped run was resumed (partial review/ frames
removed, `--acknowledge-uncertain shortlist_review`). Record and per-stage
times: benchmark/runs/step5-iceland-20260929T012602Z/NOTE.md.
- Planner (1 call) used Iceland in all 4 queries; constraint carried
  geo_requirement "Iceland" and min 1920x1080.
- 17 candidates, 5 sources analyzed, 16 excerpts, 0 continuity rejects;
  shortlist 5/5; 5 clips delivered, all 1920x1080 H.264; verify_export ok,
  request_fulfilled true. Stage sum 1458 s over two invocations.
- 1080p export costs: analysis copies are <=720p, so no clip was adoptable;
  export took 462 s for 5 clips (~92 s each, fresh 1080p section download +
  encode) vs ~16 s per adopted 720p clip (red deer run).
- Geo hint: strip labels supported 15, uncertain 1, conflicting 0. All
  queries named Iceland, so no off-place footage arrived; the exclusion path
  was not exercised live (unit-tested only).
- Contact sheets: all 5 are continuous shots of Icelandic waterfalls. Step 7
  quality gaps again: one clip has a "Beautiful World 4K" watermark and shows
  the fall only as distant mist; vision noted a small watermark on another.

### B1 batch baseline and first B2 fixes (2026-09-29)

B1 (benchmark/RESULTS-2026-09-29-batch1.md): 8 operator-written briefs, one
fixed config, live, cold. 6/8 completed unattended; 26/36 clips delivered;
by eye 10 good, 11 acceptable, 5 bad (21/26 usable; no cut inside any
clip). Completed runs average 17 min; time: analyze 27%, discover 21%,
strip labels 13%, export 12%, tile labels 9%, verify (both) 12%.

Two failures, both bugs, fixed in the working tree (uncommitted):
- R04: video 0I1hZCD7sT0's video track ends at ~129 s of an advertised
  193 s; a span at 151 s downloaded as an empty file (no streams, with or
  without -copyts; reproduced), and one failed span failed the whole run.
  Fix: probe_export_coverage raises code `no_streams` for a stream-less
  file; validate_analysis_media turns it into yt.AnalysisSpanEmpty; when
  EVERY attempt of a span raises it, the analyzer records the range as
  status `unavailable`, reason `no_video_in_span` (analysis_cache.
  NO_VIDEO_IN_SPAN) and the row stays `complete`. verify accepts only that
  exact form (all configured attempts, all acquire-stage no_streams, no
  media) as a warning (verify._unavailable_problem). Any other failure still
  fails the run as before.
- R11 (step 5 regression): 16-24 s clips but analysis windows of 9-14 s
  (tile interval + 2 s pad), so no excerpt could exist. Fix:
  analysis_plan(min_window_s) widens short windows around their centre (kept
  inside the video); a tight budget keeps fewer, evenly spaced whole windows
  instead of shrinking all; spread mode uses fewer, longer ranges.
  min_window_s = max(duration_min + 2*pad, target) (analyze.
  min_window_for_durations; 8 s for the default 4/6/12 band), recorded in
  the manifest settings; verify re-derives with settings.get("min_window_s",
  0), so runs analyzed before keep verifying (checked: Brazil
  20260927T220608Z --require-export ok; batch1 R07 ok).
- Tests: tests/test_b2_fixes.py (67, sonnet-high to spec, reviewed). Full
  suite 697 passed, 1 xfailed.
- Test-infra trap found: two concurrent pytest sessions share tmp/pytest and
  break each other (CLAUDE.md rule added).

Other B1 findings still open for B2: max_analyze_videos fixed by config
(defaults to 1) whatever n_clips (R05 8/10); serial vision calls (~9 s
each); discover 2 s sleeps and serial metadata; verify_export re-decodes the
analysis copies verify_review decoded; "european" queries do not name
European places (R09 3/6).

### B2 continued (2026-09-29, later session)

- Reruns on the fixed code: R04 2/2 verify ok (search drifted; the broken
  source was not a candidate, so the unavailable path was not exercised
  live); R11 windows were 20 s as designed but every download hit YouTube's
  "Sign in to confirm you're not a bot" (host refused after ~10 live runs;
  metadata lookups too). Live validation paused until the block lifts.
- New runner status `blocked` (yt.is_youtube_block; discover/analyze/export
  failures and stage exceptions carrying the marker): tells the orchestrator
  to wait and rerun the same command instead of reporting a pipeline
  failure. Cookies stay off (owner opt-in only).
- Vision: strip labels now concurrent like tiles; both use
  vision_wire.vision_workers() (default 4, SCENERY_VISION_WORKERS 1..8);
  results keep review order; runner call counters are locked. Expected
  saving ~2-3 min per run (tile + strip labels averaged 224 s in B1);
  NOT measured yet (needs a live run).
- tests/test_b2_blocked_vision.py (31, sonnet-high to spec); updated
  test_vision_wire's overlap test to the worker setting. Full suite 728
  passed, 1 xfailed.
- Discovery early stop: run_dry(max_candidates) stops metadata fetches once
  that many candidates are kept; the runner passes max_rank_videos (rank
  uses only the first N candidates in search order, so downstream inputs are
  identical). In B1 ~12 of ~17 metadata fetches per run were unused (~70 s
  and a dozen YouTube requests). Not part of the discover binding, so older
  runs resume without re-searching; discovery.json records max_candidates.
  Sleeps kept at 2 s: the YouTube block argues against faster requests.
- verify_export decode reuse: verify_review records decoded_media {path:
  sha256}; the runner's verify_export passes it as trusted_decodes, and
  verify skips only the strict decode of byte-identical media (hash, probe,
  marker and duration checks still run). Standalone verify decodes all.
  B1: verify_review averaged 56 s, verify_export 65 s; expected ~50 s saved
  per run, not measured live yet.
- Still open for B2: analyzed-source count vs n_clips (fold into B3
  profiles); "european" queries.

### Checkpoint reported to the owner (2026-09-29)

State: B1 done (6/8 unattended, 26/36 clips, 21/26 usable); B2 fixes done and
tested (756 passed) but not measured live because YouTube refuses the host
since ~11:40 UTC. Expected saving from B2 speedups ~3-5 min of a 17 min run
(estimate from B1 stage times, unmeasured). Open: n_clips-scaled source
count (B3). Questions put to the owner: (1) cookies as a fallback or wait;
(2) commit B2 now or after re-measurement; (3) design B3 profiles offline.
Owner answer: update the docs, then work on the YouTube fallback (B2b).
Commit timing and B3 not decided.

### B2b YouTube fallback (2026-09-29, owner: "lets work on the youtube fallback")

**STATUS: NOT YET FUNCTIONAL and OFF** (owner, 2026-09-29: keep it off and
clearly marked). Implemented and unit-tested only; never run against YouTube.

Findings: the block is IP-level. yt-dlp already runs with --js-runtimes node
(the missing-runtime warning came only from manual commands). Alternate
player clients (tv_simply; web_safari,mweb) were refused the same way
(one request each). Without an account, only waiting or another network
route helps. "sign in to confirm" was already a fatal (non-retried) yt-dlp
error, but the analyzer still retried every span of every video (~20
refused requests in R11 after the first).

Built (working tree, uncommitted):
- Circuit breaker in YtDlp._run: after a refusal, every later call in the
  process raises YOUTUBE_BLOCKED_EARLIER without contacting YouTube (the
  message carries the block marker, so the runner reports `blocked`).
- Opt-in fallback (config youtube_cookies_file / youtube_proxy; off by
  default; yt.YoutubeFallback, youtube_fallback_from_config): on the first
  refusal the call is retried once through the fallback and the process keeps
  using it; if the fallback is refused too, the breaker trips. The cookies
  file must be outside the project; each call gets a private 0600 copy in the
  run tmp dir (yt-dlp writes the jar back; 2 analysis workers), deleted
  after; errors through the fallback path redact the proxy URL and copy path.
  Wired into run, run-brief, analyze, run-pipeline and export's default
  downloader; invalid settings exit 2 / ExportError.
- Runner: result/state `youtube_fallback_used`; the `blocked` instruction
  says whether a configured fallback was refused too or how to opt in.
- NOT validated live: needs a cookies file (or proxy) from the owner; the
  host is still refused.
- Tests: tests/test_youtube_fallback.py (41; sonnet-high to spec). It found
  two defects, both fixed: (1) the redacted error was raised `from None`
  inside the except block, so `__context__` still held the original text
  with the proxy URL and cookie-copy path; now raised outside the block, no
  context. (2) a relative youtube_cookies_file resolved against the working
  directory, not the project root; now root-relative (and then refused as
  inside the project). Full suite 797 passed, 1 xfailed.

### Session wrap-up (2026-09-29, owner: document everything, keep local)

Owner: "document all work done in this session, including hand off, do not
commit, merge push or anything like that, keep everything local for now.
Make sure the youtube fall back is off and it is clearly highlighted it is
not yet functional." Done: fallback off (no config sets it, no cookies
file, example lines commented and marked NOT YET FUNCTIONAL in AGENTS.md,
config.example.yaml, CLI.md, RUNNER.md, STATUS.md, ISSUES.md, HANDOFF.md).
Nothing committed after 87f17c3. Full suite 797 passed, 1 xfailed. YouTube
still refused the host at the last check. Next steps: HANDOFF.md.


### Jev investigation and integration (2026-09-29, afternoon)

Owner: investigate TypeSafe's Jev (System One decision model) for this
project; then "yes do that": rebuild it brief-generic, wire it in, test, run
live if YouTube allows, document as you go.

- Investigation: Jev is text in, calibrated typed answers out (noul / choice /
  score), no images, no text generation, $0.042/MTok input, ~0.2 s. Fits as a
  cheap judge over metadata and over the vision model's notes; not a
  replacement for vision, planner, continuity, export or verify. The old
  exp/jev-gate (merged in PR #1) had train-only questions, ran after all
  metadata fetches, and the runner forced it off.
- Offline evaluation (benchmark/jev_eval/, README there): 184 candidates and
  116 strip notes from 10 existing runs, blind Sonnet reference labels,
  observed outcomes for 44 analyzed sources, eye verdicts for 26 clips.
  $0.05 of Jev calls, 0 errors. Rank-slot replay: search order 17/50
  reference-"no" picks, Jev 2-3/50. Outcome AUC 0.75-0.80 (reference judge
  0.745: metadata is the limit). Note check AUC 0.99 vs reference; at 0.70
  flags 2/5 bad delivered clips, 1/21 good.
- Question set v2 written from v1's misses (relaxation film vs dark screen),
  score = mean of P(usable), P(subject), P(conditions) (beat usable alone in
  every variant). Thresholds: reject <= 0.35 (0 productive lost offline),
  note violation >= 0.70.
- YouTube accepted the host again at 14:24 UTC (one check). One flat search
  confirmed hits carry a ~120-150 character description snippet.
- Built (uncommitted): src/scenery_brief_clips/jev.py (client, cache, budget,
  questions, DiscoveryJudge, apply_note_check); run_dry(judge=); runner
  jev_rank / jev_note_check with Ports.jev_post and bindings that change only
  when on; shortlist exclusion "jev note violation" via `note_violation` in
  the labels file (verify reproduces it); jev-gate rebuilt on the same
  questions (schema jev_gate_v2, --brief); config keys; docs/JEV.md (was
  JEV_GATE.md), STATUS, ISSUES, RUNNER, CLI, ARCHITECTURE, DATA, ROADMAP,
  README, AGENTS, config.example.yaml. Bench: BENCH_JEV, BENCH_SEED_METADATA,
  benchmark/jev_ab.sh (paired live A/B), summary.json "jev" block.
- Tests: tests/test_jev.py (151) and tests/test_jev_gate.py (25, rewritten),
  sonnet-high to spec; no src bugs found. Full suite 958 passed, 1 xfailed.
- Live A/B (benchmark/RESULTS-2026-09-29-jev.md): owner stopped the batch
  after the R09 pair to free the host. R09: Jev 4 clips / 2 usable vs control
  3 / 3 (blind judge). Jev fixed ranking (no Idaho/Colorado/Banff sources) but
  kept a Canadian-looking source it had itself labeled place: different (the
  score ignores place) and an illustration channel passed every stage. Next:
  place rule (offline: rejects 14/184, 0 truly on-place) and "not real
  footage" in the note criteria; then `benchmark/jev_ab.sh R07 R10 R03`.
  First live numbers for B2: discover 206 -> 113 s, label_strips 174 -> 103 s,
  verify_export 68 -> 12 s (R09, cold, vs B1).

### Local work committed and pushed (2026-09-29, evening)

Owner: "commit and push the local work". Full suite first: 958 passed, 1
xfailed. Committed on fix/analysis-download-bounds as aa315a8 (src, tests,
docs: B2, B2b, Jev) and 80c0f68 (benchmark harness, B1/Jev results, run
records) and pushed; NOT merged. Left untracked: benchmark/jev_ab/judge/
sheets/ (7 contact sheets of third-party video frames; the repo tracks no
images). Secret scan before committing: the OpenRouter key appears in no
file; the only credential-like strings are dummy fixtures in
tests/test_youtube_fallback.py.

### Unattended-first track: offline findings (2026-09-29, evening)

Owner chose the unattended-first track (A1 source count + B label budget ->
C auto-recovery -> one small live batch) and set a direction: this is a
holistic request-to-clips gatherer; place matching is NOT a priority ("if it
looks like the country, that is enough"; most requests name no place).

Offline study over the 29 local runs with a shortlist
(benchmark/unattended_eval/, run with .venv/bin/python from the repo root):
- Geography: the geo gate never excluded a moment vision marked keep (0 of
  213 keeps had geo conflicting). The cost is in `match`: the place is in
  the theme text, and vision downgrades match for place. R09 "European
  alpine lake": 9 of 12 non-keeps say "resembles the Canadian Rockies" / "not
  recognizably European" (shortfall 3 of 6; the Jev control arm the same).
  R07 is the same strictness about setting ("meadow rather than inside a
  forest", 7 uncertain). Shortlist excludes match uncertain outright.
- Label budget (label_budget.py, strip labels replayed from stored labels):
  270 labeled, 93 selected. Skipping moments that duplicate an already-kept
  moment (frame hashes exist in review.json before labeling): 204 labels,
  identical selections. Plus stopping once n_clips distinct keeps exist
  (waves of 4, sources interleaved): 170 labels (-37%), selected 94 vs 93.
- Source count: distinct keeps per analyzed source ~0.9-3.8, median ~1.75
  (batch runs). The batch config pinned max_analyze_videos 5; with no config
  the default is 1.
Nothing changed in src/ yet.

### Unattended-first track: built (2026-09-29, evening; owner "yes" to items 1-3 incl. the place rule)

1. Strip label budget (vision_wire.label_review_strips(budget=True), default
   on via config strip_label_budget; off with jev_note_check or
   `label-strips --label-all`). Waves of vision_workers(), sources
   interleaved, duplicates of a keep skipped before labeling, stop at n_clips
   distinct keeps (shortlist.label_keeps = the shortlist's own rule). Skipped
   moments go under "unlabeled" in shortlist_scores.json; build_shortlist
   uses the recorded reason, so verify reproduces it. Binding gains
   strip_budget v1 (old runs relabel from their checkpoints, no model call).
   Product replay (benchmark/unattended_eval/replay_budget.py): 270 -> 185
   labels, selected 93 -> 94, no run lost a clip. tests/test_strip_budget.py
   (120, sonnet-high to spec; I mutation-checked 3 rules: 15/6/3 failures).
2. Source scaling (runner.config_with_brief_source_count): with no config
   value, max_analyze_videos = ceil(n_clips/1.5)+1, max 12; max_rank_videos
   = analyze+4 when that exceeds 10. Explicit config wins.
   config.example.yaml no longer pins 1/10 (copying it would have disabled
   scaling). Every benchmark config still pins both: the live check needs
   configs without them. tests/test_source_scaling.py (21).
3. Place/setting soft rule (vision_wire.PLACE_SOFT_RULE, only when a brief
   theme exists; label_policy vision_label_v2 so resumed runs relabel).
   benchmark/unattended_eval/relabel_place.py, 70 stored strips, GLM:
   baseline (old prompt) 5 flips = noise (4 from the Brazil run, first
   labeled by gpt-6-sol); v2 28 flips incl. a strip whose note says it cuts
   and an R05 caption clip; v2b (+ "relaxes only place and setting, never
   cuts, titles, subject or action") 24 flips: 18 place-only alpine lakes,
   3 of 4 R07 meadow deer, the rest shared with the baseline; 13/13 controls
   kept; true rejects stayed rejected. Shipped v2b. ~200 GLM calls total.
4. Auto-recovery (runner): an interrupted external stage (inflight marker
   left by a killed process) is redone once per stage and run
   (auto_recoveries; retries counted); a second interruption, changed
   outputs, missing outputs after a finished stage, or auto_recover: false
   pause as before. Checked each external stage is safe to redo (caches,
   checkpoints, review clears moment dirs, export clears staging). Tests:
   runner contract (redo once, second pause, config off, Iceland-style
   partial review frames), reliability test updated; mutation (limit 0)
   fails 3 tests.
Full suite 1104 passed, 1 xfailed. Uncommitted. Next: small live batch.

### Unattended-first track: live batch2 (2026-09-29, evening; owner: "run all three, if acceptable commit and push")

benchmark/RESULTS-2026-09-29-batch2.md. Acceptance set before the results:
all complete unattended with verify ok; no clip-count regression (R09 > 3/6,
R05 >= 8/10, R07 4/4); most clips usable, no cuts. Outcome: verify ok on all;
R05 10/10, R07 4/4, R09 3/6 (criterion missed: not a regression, cause
upstream: 6 of 10 moments duplicates, 5 sources); 16/17 usable by eye, no
cuts. R09 and R05 were `blocked` by brief YouTube refusals and resumed
(benchmark/resume.sh, new); R05's resume made the first live auto-recovery.
Judged acceptable with the R09 miss recorded; committed and pushed.
