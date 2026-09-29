# HANDOFF (sessions of 2026-09-28 and 2026-09-29)

Branch: `fix/analysis-download-bounds`, everything committed and pushed to
origin (owner, 2026-09-29: "commit everything and push but not merge").
Nothing is merged to `master` (master = eedb911); do not merge without the
owner asking.

- Plan steps 1-2, the benchmark harness, export preset, verify
  compatibility: ba4779d and earlier.
- Plan step 3 (the `blend` continuity detector, now the default) and plan
  step 4 (planner prompt v2, retry, `run-pipeline --live-planner`), bench.sh
  options, evaluation data: the "feat: blend continuity detector" commit.
- Plan step 5 (brief form loosened per owner decisions, validated live:
  Iceland 1080p 5/5, verify ok) plus the standalone export/analyze cap fix:
  the "feat: loosen the brief form (plan step 5)" commit.

Read with: `docs/PLAN-PROGRESS-2026-09-28.md` (running log; "Status per step"
after the ground rules and harness sections), `benchmark/RESULTS-2026-09-28.md` and
`benchmark/RESULTS-2026-09-29-step4.md` (all numbers),
`benchmark/continuity_eval/README.md` (gate evaluation), and
`docs/HANDOFF-2026-09-28.md` (the earlier review whose "Recommended plan" these
sessions work through). Project rules: AGENTS.md, CLAUDE.md.

## Goal

Project: take an informal request ("cinematic clips of horses grazing") and
deliver high-quality clips that match it, as files, without an agent having to
babysit stages.

Session goal (owner): work through the "Recommended plan" in
`docs/HANDOFF-2026-09-28.md` one step at a time for faster runs, higher yield
and quality, and no babysitting. After each step: tests, a fixed benchmark,
compare timings AND yield, report honestly. Host: 1 CPU, 1.6 GB RAM.

## State: done and working

Tests: `.venv/bin/python -m pytest tests/ -q` gives 630 passed, 1 xfailed on
the current working tree (about 6 min): 465 at the step 3-4 commit, plus 30
tests in tests/test_brief.py that pytest never collected before (class names),
plus 112 step-5 tests, plus 23 for the brief export cap in standalone
commands (tests/test_export_cap_request.py). The xfail is a known limitation (synthetic fast zoom flags a
dissolve in the blend detector; on real footage such false alarms only trim).

1. **Benchmark harness** `benchmark/bench.sh <label> [seed_run_dir]`: fresh
   project root under `tmp/bench/` (cold caches), `run-pipeline` with live
   vision (`z-ai/glm-5.3-flash`), `SCENERY_ANALYZE_WORKERS=2`; summary in
   `benchmark/runs/<label>-<utc>/summary.json`.
   - Inputs (`BENCH_INPUTS=`): `improvement` (ocean, default), `military`
     (edited parade footage), `heldout-horses`, `step4-reddeer`.
   - Seeded runs reuse discover..apply_scores from a baseline so analysis and
     export see identical inputs (live search drifts).
   - Env: `BENCH_DETECTOR=blend|legacy` overrides the continuity detector;
     `BENCH_NO_EXPORT=1` stops at agree_export; `BENCH_LIVE_PLANNER=1` plans
     with planner.yaml instead of the frozen plan (inputs may omit plan.json).
2. **Step 1, runner bounds** (committed): analyze 2 workers; span download
   timeout 90 s + 10 s per span second (max 300 s); `run_deadline_s` (default
   10800) ends an invocation with status `deadline`, resumable.
3. **Step 2, acquire once** (committed): cache policy `v4-copyts-720`
   (stream-copy `-copyts` sections, one yt-dlp call per video, export
   rendition pinned when <=720p, local 0 = first packet PTS via
   `first_pts_ms`/`mapping_k_s`); export adopts the analysis copy; continuity
   walks forward from one seek. Analyze ocean 525 -> 116 s (seeded), military
   980 s (cold live baseline) -> 245 s (seeded, host partly contaminated);
   clean seeded military on current code: 156 s (legacy gate) / 160 s (blend).
   Yield unchanged. Not done from the original step 2: a single decode pass
   shared by scene detection, continuity and review frames. Export preset `faster`
   (`x264-crf17-faster-v1`; verify still accepts `-medium-v1`). Verify checks
   each run under its recorded cache policy (v4, v3); the Brazil run
   `data/runs/20260927T220608Z` verifies with `--require-export`.
4. **Step 3, continuity** (in the "feat: blend continuity detector" commit). The yield loss was the legacy gate
   rejecting camera motion. New detector `src/scenery_brief_clips/
   continuity_blend.py` (6 fps, 160 px, 1 s pad; motion-compensated cut test +
   cross-dissolve blend fit; medians skip frozen pairs; both samples around a
   cut are excluded). **Default since 2026-09-29** (owner approved);
   `continuity_detector: legacy` restores the old gate unchanged.
   - Labeled set (66 windows, replayed through the final product code on
     2026-09-29, `benchmark/continuity_eval/labeled/replay_blend.json`): blend
     keeps 0/16 transitions, rejects 2/50 continuous shots (one whip pan, same
     window in two runs) and trims 2/50; legacy rejected 25/50, trimmed 9/50.
     All 6 transition trims exclude the transition (one checked frame by
     frame: a 5-frame dissolve).
   - Held-out live horses run (22 windows): blend 0 errors; legacy kept 3
     loop-seam jump cuts.
   - Seeded military: excerpts 3 -> 7, still 2/2 delivered; gate +13-16
     worker-seconds per run, analyze wall within noise.
   - Runner binding follows the effective detector: runs analyzed before the
     flip are re-analyzed on resume unless their config pins legacy.
5. **Step 4, planner** (in the "feat: blend continuity detector" commit). Prompt v2 (`brief.PLANNER_INSTRUCTION`,
   version `search_query_planner_v2`) states the validator's rules; one
   corrective retry with the rejection (`planner.plan_queries`, provenance
   `model_calls`, `rejected_attempts`); `run-pipeline --live-planner
   [--planner-config]`; runner no longer crashes on a live plan (wire was
   None). Live GLM, 9 briefs x 3: first-attempt valid 8/27 -> 25/27; with
   retry 27/27. First one-command run on a new theme (red deer, cold, with
   export): completed 2/2 in 5:27, verify ok, no operator input.
6. **Step 5, brief form** (committed and pushed; details in PLAN-PROGRESS "Step 5").
   Owner approved: duration band 2-30 s, export 720 or 1080, `sources` needs
   only subjects + n_clips, free-text geography; kept: 1280x720 floor, 16:9
   band, search caps, no download. Geography design (chosen by me, owner left
   the amount open): the place goes to the planner and, as a soft hint, to
   vision; only clips that clearly show a different kind of place are
   excluded (existing geo "conflicting" rule). Also fixed: the brief's export
   cap was never used (now effective via run-pipeline), "european" was never
   enforced (unchanged), and tests/test_brief.py's 29 tests were never
   collected (class names). Contract doc docs/SEARCH_BRIEF.md rewritten.
   Operator-agent eval on 12 plain requests: valid briefs 2/10 before, 9/9
   after, with correct questions for missing count, 4K and vertical
   (benchmark/brief_eval/).
   Live check (resumed run, benchmark/runs/step5-iceland-20260929T012602Z/
   NOTE.md): "5 clips of waterfalls in Iceland, 1080p" delivered 5/5 at
   1920x1080, verify ok, all continuous Icelandic waterfalls on contact
   sheets. Geo labels: supported 15, uncertain 1, conflicting 0 (the
   exclusion path was not exercised: every query named Iceland). 1080p
   export is slow on this host: nothing is adoptable from the <=720p analysis
   copies, 462 s for 5 clips vs ~16 s per adopted 720p clip.
   Follow-up fix: the brief's cap is recorded in discovery.json and the
   standalone `export`/`analyze` commands apply it (same rule as
   run-pipeline); runs without the record use config alone.

## Caveats and known gaps

- The blend dissolve thresholds have thin margins (weakest dissolve score
  1.42 vs strongest continuous 1.24 on the tuning set) and the held-out set had
  no dissolves. Watch new edited themes; if a dissolve slips through, add it to
  the labeled set and re-tune with `benchmark/continuity_eval/`.
- Blend costs more gate CPU (+35-90% on that phase) and yields more candidates,
  so shortlist review takes longer; overall wall was ~+1 min on the benchmarks.
- The planner prompt gained one sentence (irregular plurals) after the 9-brief
  measurements; PLANNER_INSTRUCTION_VERSION stayed `search_query_planner_v2`.
  See benchmark/planner_eval/README.md. Bump the version on the next prompt
  change.
- "goose" still needs the planner retry most times (model writes "geese");
  the validator accepts only the noun as written or plus "s". Accepting
  irregular plurals would save calls but is a validator policy change: ask the
  owner.
- Quality gaps (plan step 7), seen in the red deer run: a "nature" watermark;
  a clip vision kept that looks like a white-tailed deer and possibly rendered
  imagery. Also: shortlist diversity does not prefer distinct sources; brief
  exclusions are not enforced.
- Benchmark noise about +-15% per stage. Step-2c timings were inflated by a
  Hermes headless Chrome on the host; clean legacy military is 4:57 wall.
- Branch/skill mismatch still stands: `.hermes/skills/scenery-clips/SKILL.md`
  step 1 requires HEAD == origin/master, and the skill does not use
  `run-pipeline` (plan step 6).

## Decisions (and why)

- Reuse one download for analysis and export: owner approved, gates hold.
- Canonical model wire: GLM 5.3 Flash on OpenRouter for vision and planner
  (owner). Key `OPENROUTER_API_KEY` in `~/.hermes/.env`; never print or write it.
- Encode preset `faster`, not `veryfast` (3x the quality loss for little speed).
- No keep-ratio gate: the Brazil data contradicts it.
- Continuity: changed only with labeled-set numbers and owner approval
  ("build it, off by default", then "make blend default").
- Planner: state the validator's rule in the prompt and retry once, rather
  than loosen the validator.
- Brief form (step 5, owner 2026-09-29): loosen duration, export height,
  provenance defaults and geography; keep the 1280x720 floor, 16:9 band,
  search caps and no-download. Geography is a soft check, not a hard gate:
  requiring vision to confirm a place would collapse yield (most scenery is
  not recognizable) and overclaim. 4K and vertical stay unsupported; the
  contract tells the operator to ask.
- Tried and dropped: lagged/masked residual (signals.py v2) did not separate
  dissolves from motion; first continuity labels were on buggy sheets
  (output-side `-t`); running pytest or two benchmarks concurrently.

## Next steps, in order

1. Ask the owner whether to merge the branch to master (not approved yet).
2. SKILL.md step 5 still says "Ignore that file's 'not implemented' header"
   (harmless; fix with step 6). The owner said to leave Hermes/step-6 work
   alone for now (2026-09-29).
3. Plan step 6: make the Hermes skill one command (`run-pipeline --brief B
   --live-planner --vision-agree --live-vision --allow-export`), with the
   agreements asked up front; fix the skill's origin/master check.
4. Plan step 7: quality signals on review frames (sharpness, watermark/text,
   rendered imagery, subject species).

## Commands

```bash
.venv/bin/scenery-brief-clips doctor
.venv/bin/python -m pytest tests/ -q          # ~6 min; never alongside a benchmark
# before timing anything: uptime; ps aux --sort=-%cpu | head  (leave hermes gateway alone)
benchmark/bench.sh stepN-ocean $PWD/tmp/bench/baseline-20260928T154614Z/data/runs/20260928T154616Z
BENCH_INPUTS=military benchmark/bench.sh stepN-military $(ls -d $PWD/tmp/bench/military-base-*/data/runs/*)
BENCH_INPUTS=step4-reddeer BENCH_LIVE_PLANNER=1 benchmark/bench.sh reddeer   # cold, one command, live
BENCH_INPUTS=heldout-horses BENCH_NO_EXPORT=1 benchmark/bench.sh horses       # live, stops before export
# Live runs cost real yt-dlp traffic and model calls (small). The owner
# approved them for benchmarking with the GLM wire; ask before new kinds of spend.
# one full run from a brief, no plan file
.venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --live-planner \
    --config config.yaml --vision-agree --live-vision --allow-export --theme NAME
.venv/bin/scenery-brief-clips verify --run-dir data/runs/<id> --require-export
# gate evaluation: benchmark/continuity_eval/README.md
# planner evaluation: benchmark/planner_eval/README.md
```

Do not delete the seed roots `tmp/bench/baseline-20260928T154614Z` and
`tmp/bench/military-base-20260928T165504Z` (seeded benchmarks), nor the
analysis copies under `data/cache/analysis` and `tmp/bench/*/data/cache/
analysis` that the continuity evaluation replays. All are gitignored and
exist only on this host.
