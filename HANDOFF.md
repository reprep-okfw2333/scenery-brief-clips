# HANDOFF (session of 2026-09-28)

Branch: `fix/analysis-download-bounds`. Everything below is committed on that
branch. Nothing is merged to `master` and nothing is pushed.

Read with: `docs/PLAN-PROGRESS-2026-09-28.md` (the running log of this
session), `benchmark/RESULTS-2026-09-28.md` (all numbers), and
`docs/HANDOFF-2026-09-28.md` (the earlier review whose plan this session worked
through).

## Goal

Project: take an informal request ("cinematic clips of horses grazing") and
deliver high-quality clips that match it, as files, without an agent having
to babysit stages.

Goal of this session (set by the owner): work through the "Recommended plan"
in `docs/HANDOFF-2026-09-28.md` one step at a time for faster runs, higher clip
yield and quality, and no babysitting. After each step: run the tests, rerun a
fixed benchmark, compare timings and yield, and report. Do not pad results or
explain a shortfall away. The host has 1 CPU and 1.6 GB RAM, so keep parallel
downloads low.

## Finished and working

Tests: `.venv/bin/python -m pytest tests/ -q` gives 405 passed (391 at the
start of the session).

1. **Benchmark harness.**
   - `benchmark/bench.sh <label> [seed_run_dir]` gives every run a fresh
     project root under `tmp/bench/`, so all caches start cold. It runs
     `run-pipeline` with a frozen brief and plan, live vision
     (`z-ai/glm-5.3-flash`) and `SCENERY_ANALYZE_WORKERS=2`, then
     `benchmark/summarize.py` writes `benchmark/runs/<label>-<utc>/summary.json`.
   - Two input sets: `benchmark/improvement/` (2 ocean-wave clips, the default)
     and `benchmark/military/` (`BENCH_INPUTS=military`: 2 clips of Brazilian
     military parades, edited footage).
   - "Seeded" runs reuse discover..apply_scores and the metadata cache from a
     baseline run. Live search results drift, so seeding keeps the inputs to
     analysis and export identical between runs.
2. **Plan step 1** (runner bounds).
   - Analyze defaults to 2 workers (`pipeline_analyze.DEFAULT_ANALYZE_WORKERS`).
   - Each analysis span download has a hard timeout of 90 s + 10 s per span
     second, capped at 300 s (`yt.analysis_timeout_s`).
   - Each runner invocation has a wall-clock budget, `run_deadline_s` (config,
     default 10800). When it runs out, the runner returns status `deadline`;
     rerunning the same command resumes (`runner.py`, `docs/RUNNER.md`).
3. **Plan step 2** (acquire once, decode less). Cache policy is now `v4-copyts-720`.
   - Analysis copies are stream-copied `--download-sections` with `-copyts`.
     There is no `--force-keyframes-at-cuts` re-encode.
   - All spans of one video come from one yt-dlp call
     (`YtDlp.prefetch_analysis`). The export rendition is pinned when it is
     ≤720p (`export.analysis_format_id`).
   - Local time 0 is the first packet PTS: marker `first_pts_ms`, range
     `mapping_k_s`. The pipeline, continuity, review and verify all use it, and
     verify proves it from the file, the marker and the record.
   - Export adopts a matching analysis copy instead of downloading again
     (`export._adoptable_analysis_copy`, `YtDlp.fetch_export(source=)`, plan
     field `acq_source`). Acceptance is unchanged, and verify checks the
     adopted digest.
   - Continuity sampling walks forward from one seek instead of seeking per
     sample. It picks identical frames (checked on 51/51 real windows) and
     `SCENERY_CONTINUITY_SEEK_EACH=1` restores the old per-sample seek.
   - A proven cache hit skips its second full decode.
   - Owner approved reusing one download for both analysis and export. See the
     amendments in `docs/EXPORT.md` and `docs/ARCHITECTURE.md`.
4. **Export encode preset `faster`** (recipe `x264-crf17-faster-v1`). The owner
   asked me to pick after measuring. It is 1.35-1.85x faster than `medium`,
   files are 3% larger, and quality drops about 0.1 dB PSNR. Verify still
   accepts exports published under `x264-crf17-medium-v1` (`ACCEPTED_RECIPES`).
5. **Backward compatibility of verify.** Verify checks each run under the
   analysis cache policy its manifest recorded (`ACCEPTED_ANALYSIS_POLICIES` =
   v4, v3). The real Brazil run `data/runs/20260927T220608Z` verifies
   `ok: true` with `--require-export` (16 acquisitions checked).

Results. Yield was unchanged in every run: ocean 5 excerpts, 2/2 clips;
military 3 excerpts, 2/2 clips, the same clips each time.

| | before | after step 2 |
|---|---|---|
| ocean analyze | 451-525 s | 116 s |
| military analyze | 980 s | 245 s |
| ocean wall | 13:16 | about 6-7.5 min |
| military wall | 23:31 | about 9-12 min |

## Half done: plan step 3, and exactly where it stopped

The handoff's step 3 ("merge nearby kept tiles into wide regions; require ≥2
kept tiles or a keep ratio") was tested against data before being built.

- **Keep gate: dropped.** In the Brazil run, keep count and keep ratio do not
  predict yield. A ratio ≥0.5 gate would have lost 3 of the 16 delivered
  clips.
- **Wider windows: not built.** In the military benchmark, PySceneDetect finds
  no cut in any of the 10 spans; each is already one shot. Wider windows would
  not help.
- **The real yield loss is continuity-gate false rejects.** The gate reads
  camera motion (pans over marching troops) as a transition.

Evaluation set: `benchmark/continuity_eval/`. Data is saved in `labeled/`; the
contact sheets are in `tmp/continuity_eval/` (regenerate them with
`build_set.py`). It holds 66 real gate decisions from the Brazil run plus the
military 2c run, labeled blind by a Sonnet sub-agent on corrected sheets and
spot-checked by me: 52 continuous, 14 transition.

- **Current gate:** misses 0 of 14 transitions, but flags 36 of 52 continuous
  shots (26 outright rejects).
- **Best simple candidate** (6 fps, motion-compensated residual jump ≥15 that
  is also ≥3.5x the window median; `score.py`): misses 3/14 transitions and
  flags 3/52 continuous shots. The 3 misses are real cross-dissolves: two on a
  news broadcast with static overlays, and one stock montage.

Stopped at: `benchmark/continuity_eval/signals.py` was just extended with a
1-second-lag residual and a static-overlay mask (`compensated_residual`,
`dynamic_mask`; output `signals_6fps_v2.json`). The idea is to catch dissolves,
which spread over 0.5-1 s, while tolerating sustained motion. **This extension
has never been run.** No product code for a new gate exists yet. The owner has
not approved changing the gate.

## Broken, untested, or caveats

- `signals.py` v2 code (above) is untested.
- Step 2c timings (export 166-171 s, verify_review 118 s) are contaminated: a
  Hermes agent ran headless Chrome on the host during those runs. Clean
  measurements say adoption removes the download (about 20-27 s per clip) and
  leaves the encode unchanged. See `benchmark/RESULTS-2026-09-28.md`.
- Benchmark noise: analyze varied 451-525 s on unchanged code. Treat
  differences under about 15% as noise.
- The `faster` preset and export adoption have been benchmarked. There has been
  no full live end-to-end run of a new theme on the final code.
- Known quality gaps, not fixed:
  - Shortlist diversity does not prefer distinct sources.
  - Brief exclusions are not enforced.
  - The "trim" verdicts on transitions were not checked for whether the
    trimmed span really excludes the transition.
- Branch/skill mismatch from the earlier review still stands:
  `.hermes/skills/scenery-clips/SKILL.md` step 1 requires HEAD == origin/master,
  and the skill does not use `run-pipeline`.

## Decisions (and why)

- **Reuse one download for analysis and export.** Owner approved it on the
  condition that the gates hold, which they do. It relaxes the old "never
  substitute" rule.
- **Canonical model wire:** GLM 5.3 Flash on OpenRouter, for both vision and
  planner. Owner's choice.
- **Encode preset `faster`, not `veryfast`.** `veryfast` has 3x the quality
  loss and 7-16% larger files for only a little more speed.
- **No keep-ratio gate.** The data contradicts it (see above).
- **Continuity gate:** not loosened by threshold tweaks. Any change must be
  measured on the labeled set and approved by the owner, because docs say not
  to weaken gates.
- **Tried and dropped:**
  - The first continuity labels. They were on buggy sheets: an output-side
    `-t` let `tile` read past the window. They were discarded and relabeled.
  - Running pytest or two benchmarks concurrently on 1 CPU. It distorts every
    number.
- **Test changes made during the session:** a racy test
  (`tests/test_vision_checkpoints.py`) was made deterministic; the fakes'
  `fetch_span` gained `format_id=`.

## Next steps, in order

1. Run `signals.py` v2 (`.venv/bin/python benchmark/continuity_eval/signals.py
   tmp/continuity_eval 6 160`), extend `score.py` to use `res_lag1s_masked` /
   `res1_masked` spikes, and look for a rule with 0 missed transitions and few
   false rejects. Validate it on fresh footage (a new-theme run) rather than
   only these 66 items.
2. Show the owner the numbers. If approved, implement the gate in
   `continuity.py` behind a setting, keeping the old gate available. Rerun both
   seeded benchmarks and a fresh live theme, and report yield.
3. Plan step 4: the planner prompt states the exact-subject rule, a failed plan
   is retried once with the validator error, and the live planner is wired
   into `run-pipeline` (`brief.py`, `runner.py`, `cli.py`).
4. Plan step 5: loosen the brief form (duration band e.g. 2-15 s; fewer
   mandatory provenance fields; `brief.validate_brief`).
5. Plan step 6: make the Hermes skill one command (`run-pipeline
   --live-vision` with agreements up front), and fix the skill's
   origin/master check. Then ask the owner about merging to master.

## Commands

```bash
# env / sanity
.venv/bin/scenery-brief-clips doctor
# tests (about 6 min on this host; do not run alongside a benchmark)
.venv/bin/python -m pytest tests/ -q
# benchmarks (key read from OPENROUTER_API_KEY or ~/.hermes/.env)
benchmark/bench.sh baseline-new                               # cold, live discovery
benchmark/bench.sh stepN-ocean  $PWD/tmp/bench/baseline-20260928T154614Z/data/runs/20260928T154616Z
BENCH_INPUTS=military benchmark/bench.sh stepN-military $(ls -d $PWD/tmp/bench/military-base-*/data/runs/*)
# one full run
.venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --plan PLAN.json \
    --config config.yaml --vision-agree --live-vision --allow-export --theme NAME
.venv/bin/scenery-brief-clips verify --run-dir data/runs/<id> --require-export
# continuity evaluation
.venv/bin/python benchmark/continuity_eval/build_set.py tmp/continuity_eval data/runs/20260927T220608Z <more run dirs>
.venv/bin/python benchmark/continuity_eval/score.py tmp/continuity_eval   # needs labels_sonnet.json + signals
```

Do not delete the two seed roots `tmp/bench/baseline-20260928T154614Z` and
`tmp/bench/military-base-20260928T165504Z`. The seeded benchmarks depend on
them. They are gitignored, so they exist only on this host.
