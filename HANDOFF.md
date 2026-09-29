# HANDOFF (sessions of 2026-09-28 and 2026-09-29)

> **YouTube fallback: NOT YET FUNCTIONAL, OFF.** The opt-in cookies/proxy
> fallback (config `youtube_cookies_file` / `youtube_proxy`) is implemented
> and unit-tested only; it has NEVER been exercised against YouTube. No
> config on this host sets it and no cookies file exists. Do not rely on it
> or describe it as working until a live run with owner-supplied cookies or
> proxy proves it. The always-on part (circuit breaker + status `blocked`)
> is also unit-tested only.

Branch: `fix/analysis-download-bounds`, MERGED into `master` by fast-forward
on 2026-09-29 (owner: "can you merge now?"; before: master = eedb911). Checks
before the merge: master had no commits of its own (fast-forward, no
conflicts); full suite 1104 passed, 1 xfailed on the exact head; standalone
`verify --require-export` ok on the pre-branch Brazil run and on batch2 R07
and R05; `doctor` clean. New work: branch from master.

- **Committed and pushed** (owner, 2026-09-29: "commit everything and push
  but not merge"):
  - ba4779d and earlier: plan steps 1-2, benchmark harness, export preset,
    verify compatibility.
  - 57f29d3 "feat: blend continuity detector": plan steps 3-4.
  - 87f17c3 "feat: loosen the brief form (plan step 5)": step 5 + the
    standalone export/analyze cap fix, Iceland validation record.
  - aa315a8 "feat: B2 reliability/speed fixes, YouTube block handling, Jev
    ranking" and 80c0f68 "benchmark: batch and Jev A/B harness ..." (owner,
    2026-09-29 evening: "commit and push the local work"): B1 records, B2,
    B2b, Jev, docs. The Jev judge contact sheets
    (benchmark/jev_ab/judge/sheets/, third-party frames) stay untracked.
  Each future commit/push still needs the owner asking.

Read with: `docs/PLAN-PROGRESS-2026-09-28.md` (running log; "Status per
step", then "Priorities and new order" and the B1/B2/B2b sections at the
end), `benchmark/RESULTS-2026-09-29-batch1.md` (batch numbers and clip
judgement), `benchmark/RESULTS-2026-09-28.md`,
`benchmark/RESULTS-2026-09-29-step4.md`, `benchmark/continuity_eval/README.md`
and `docs/HANDOFF-2026-09-28.md` (the earlier review whose "Recommended plan"
these sessions started from). Project rules: AGENTS.md, CLAUDE.md.

## Goal

Project: take an informal request ("cinematic clips of horses grazing") and
deliver high-quality clips that match it, as files, without an agent having to
babysit stages.

Session goal (owner): work through the "Recommended plan" in
`docs/HANDOFF-2026-09-28.md` one step at a time for faster runs, higher yield
and quality, and no babysitting. After each step: tests, a fixed benchmark,
compare timings AND yield, report honestly. Host: 1 CPU, 1.6 GB RAM.

Owner priorities (2026-09-29, later): the system should run unattended,
overseen by an AI orchestrator, from any request (generic "horses grazing" to
specific scenes, with or without a place) to individual clips. Current
priority: fast, bug-free, cheap, consistent runs. Accepted: sacrificing
specific-request enforcement (setting/elements/exclusions) and watermark
detection, and some imperfect clips if most are good. Idea from the owner:
brief/vision profiles by request specificity, with the orchestrator offering
options and asking questions before writing the formal brief (B3, not
started). No Hermes/skill (plan step 6) work for now.

## State: done and working

Tests: `.venv/bin/python -m pytest tests/ -q` gives 1104 passed, 1 xfailed on
the current working tree (about 6 min; +146 for the unattended track:
tests/test_strip_budget.py 120, tests/test_source_scaling.py 21, runner
recovery and vision prompt tests; 958 before it; 797 before the Jev work, +161 in
tests/test_jev.py and the rewritten tests/test_jev_gate.py): 465 at the step 3-4 commit, plus 30
tests in tests/test_brief.py that pytest never collected before (class names),
plus 112 step-5 tests, plus 23 for the brief export cap in standalone
commands (tests/test_export_cap_request.py), plus 67 + 31 + 24 + 4 for B2
(tests/test_b2_fixes.py, tests/test_b2_blocked_vision.py,
tests/test_b2_verify_reuse.py, early-stop tests in tests/test_pipeline.py),
plus 41 for the YouTube fallback (tests/test_youtube_fallback.py). Never run two
pytest processes at once (shared tmp/pytest). The xfail is a known limitation (synthetic fast zoom flags a
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
7. **B1 batch baseline** (committed 80c0f68; benchmark/RESULTS-2026-09-29-
   batch1.md). `benchmark/batch.sh batch1` runs 8 operator-written briefs
   (benchmark/batch1/, one fixed config) through bench.sh;
   `benchmark/batch_summary.py` tabulates. Result on 87f17c3: 6/8 unattended,
   26/36 clips, verify ok on every completed run, ~17 min per completed run,
   285 model calls. By eye (Sonnet contact-sheet labels, 2 checked by me):
   10 good, 11 acceptable, 5 bad; no cut inside any clip. Time: analyze 27%,
   discover 21%, strip labels 13%, export 12%, tile labels 9%, verify 12%.
8. **B2 fixes and speedups** (committed aa315a8, unit-tested; PLAN-PROGRESS "B1
   batch baseline and first B2 fixes", "B2 continued"):
   - Long clips (16-30 s) could never be found (step 5 regression: 9-14 s
     analysis windows). `analysis_plan(min_window_s)`, min_window_s =
     max(duration_min + 2*pad, target), recorded in the analysis manifest;
     verify reads it with default 0, so older runs still verify (Brazil run
     and batch runs checked). Live rerun of R11 showed 20 s windows.
   - A source whose video track ends early (0I1hZCD7sT0: video stops ~129 s
     of 193 s) failed the whole run. Such spans (every attempt returns a
     stream-less file) are now `unavailable` / `no_video_in_span`; verify
     accepts only that exact form, as a warning. Not exercised live (search
     drifted on the rerun).
   - Runner status `blocked` for YouTube's bot check (discover, analyze,
     export, stage exceptions).
   - Vision labels: tiles and strips run 4 calls at once
     (`SCENERY_VISION_WORKERS`, 1..8), results in review order.
   - Discovery stops fetching metadata once max_rank_videos candidates are
     kept (rank only uses those); discovery.json records max_candidates.
   - verify_export reuses verify_review's strict decode of byte-identical
     analysis media (runner path only; standalone verify decodes all).
   - Expected saving ~3-5 min per run (estimate from B1 stage times). First
     live numbers (R09, cold, vs B1; one run each): discover 206 -> 113 s,
     label_strips 174 -> 103 s, verify_export 68 -> 12 s.
9. **B2b YouTube fallback** (committed aa315a8; PLAN-PROGRESS "B2b YouTube
   fallback"). **NOT YET FUNCTIONAL / OFF** (see the box at the top).
   - Always on: circuit breaker in `YtDlp._run`: after the first refusal the
     process stops contacting YouTube (`YOUTUBE_BLOCKED_EARLIER`), run ends
     `blocked`. Before, R11 made ~20 more refused requests after the first.
   - Opt-in, off by default, never validated live: `youtube_cookies_file`
     (Netscape cookies.txt outside the project; per-call private 0600 copy)
     and `youtube_proxy`; used only after a refusal; redacted errors; result
     field `youtube_fallback_used`. Account-free alternatives were tried and
     failed (alternate player clients were refused; js runtime already set).

10. **Jev** (committed aa315a8; docs/JEV.md, PLAN-PROGRESS "Jev investigation and
   integration"). Owner asked to investigate TypeSafe's Jev, then to rebuild
   and wire it. All OFF by default:
   - `jev_rank`: discovery scores search hits (title, channel, duration,
     views, snippet) and fetches metadata in score order; rejects a
     candidate at score <= 0.35 after its metadata (`jev_reject`).
   - `jev_note_check`: vision keep notes that report a failure are excluded
     ("jev note violation"; flag stored in shortlist_scores.json so verify
     reproduces it).
   - `jev-gate` rebuilt on the same brief-generic questions (--brief).
   - Offline (benchmark/jev_eval/): rank-slot replay 17/50 -> 2-3/50
     reference-"no" picks. Live (benchmark/RESULTS-2026-09-29-jev.md), one
     pair only (owner stopped the rest to free the host): R09 Jev 2 usable
     vs 3 without. The score ignores Jev's own place answer, and an
     illustration channel passed every stage.

11. **Unattended-first track** (2026-09-29 evening, UNCOMMITTED; owner chose
   it and set the direction "holistic gatherer; place matching is not a
   priority, looks like the country is enough"). PLAN-PROGRESS
   "Unattended-first track"; offline evidence in benchmark/unattended_eval/.
   - Strip label budget (default on, `strip_label_budget`): label in waves,
     sources interleaved, skip duplicates of a keep, stop at n_clips distinct
     keeps; skipped moments recorded under `unlabeled` so verify reproduces
     the shortlist. Product replay on 29 runs: 270 -> 185 labels, 93 -> 94
     selected, no run lost a clip. tests/test_strip_budget.py (120).
   - Source count scales with n_clips when config is silent:
     max_analyze_videos = ceil(n/1.5)+1 (max 12), rank = that + 4 when > 10.
     config.example.yaml no longer pins 1/10. tests/test_source_scaling.py.
   - Places and settings are soft in the vision prompt (PLACE_SOFT_RULE,
     label policy vision_label_v2): offline relabel of 70 stored strips,
     21 place/setting doubts became keeps beyond model noise, 13/13
     controls kept, overlays/missing subjects/wrong actions still rejected.
   - An interrupted external stage is redone once automatically
     (`auto_recoveries`; `auto_recover: false` restores the pause).
   - Live (benchmark/RESULTS-2026-09-29-batch2.md, R05/R07/R09 vs B1):
     R05 10/10 (B1 8/10), R07 4/4 in 10:34 (13:07), R09 3/6 (same; pool
     of distinct moments too small, 6 of 10 were duplicates). Verify ok on
     all; by eye 16/17 usable (B1 11/15), no cut in any clip. First live
     auto-recovery (R05 discover). Two runs hit brief YouTube refusals
     (`blocked`) and were resumed with benchmark/resume.sh.

## Caveats and known gaps

- **YouTube accepted the host again** at 14:24 UTC 2026-09-29 (it had
  refused it from ~11:40 after ~10 live runs in a few hours). 2 more live
  runs followed (Jev A/B, 14:47-15:18). Keep checking rarely with ONE
  `yt-dlp --ignore-config --skip-download --print id <url>` call and pace
  live runs.
- **The YouTube fallback is not yet functional** (never run live); the
  unavailable-span path is unit-tested only.
- Pacing: ~10 live runs in a few hours triggered the block. Space live
  batches out; do not lower discovery's 2 s sleep.
- Remaining B1 gaps: the analyzed-source count does not grow with n_clips
  (R05 8/10; `max_analyze_videos` defaults to 1 with no config); "european"
  queries do not name European places (R09 3/6); every excerpt is labeled
  even when few clips are needed; 1080p export costs ~92 s per clip.
- Never run two pytest processes at once (shared tmp/pytest; CLAUDE.md).

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
- The Hermes skill (`.hermes/skills/scenery-clips/SKILL.md`) requires HEAD ==
  origin/master, which holds again since the merge; it still drives the
  step-by-step commands, not `run-pipeline` (plan step 6, on hold).

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
- Owner priorities (2026-09-29): unattended fast/cheap/bug-free runs first;
  specific enforcement and watermarks deprioritized; no Hermes work yet.
- Min analysis window: widen and keep fewer whole windows rather than let
  every window shrink below a clip; old runs verify via default 0.
- Unavailable spans: accepted only for the exact proven signature (every
  attempt stream-less); every other acquisition failure still fails the run.
- Discovery early stop without changing the discover binding, so older runs
  resume without re-searching.
- YouTube: circuit breaker always on (refused requests prolong blocks);
  cookies/proxy only as an explicit opt-in used after a refusal. Owner
  (2026-09-29): keep the fallback OFF and documented as not yet functional.
- Post-87f17c3 work was kept local, then committed and pushed on the owner's
  request (2026-09-29 evening); merged into master later that evening.
- Tried and dropped: lagged/masked residual (signals.py v2) did not separate
  dissolves from motion; first continuity labels were on buggy sheets
  (output-side `-t`); running pytest or two benchmarks concurrently.

## Next steps, in order

Owner priorities (2026-09-29): fast, bug-free, cheap, unattended runs first;
specific-request enforcement and watermarks deprioritized; no Hermes/step-6
work for now. Use a Sonnet sub-agent for repetitive work and keep docs
current with every change. Full order: PLAN-PROGRESS "Priorities and new
order".

0. Follow-ups from batch2 (RESULTS-2026-09-29-batch2.md), suggested order:
   (a) top up with more sources when the shortlist is short (R09);
   (b) tie max_rank_videos to the analyzed count when config is silent
   (rank 10 labels tiles of videos never analyzed); (c) one wait-and-retry
   on a YouTube refusal before the breaker trips (refusals cleared within
   minutes); (d) owner call: soft setting lets subject-true clips through
   without the setting (R07 stag in bracken).
1. Jev follow-up (if the owner wants it). DROPPED by the owner's direction
   (2026-09-29, place is not a priority): the Jev place rule (reject when
   P(place=different) >= 0.5). Still possible: "not real camera footage
   (painting, illustration, render)" in the note criteria (bump
   jev_note_q_v1). Then run the remaining pairs,
   `benchmark/jev_ab.sh R07 R10 R03` (~2 h, spaced; C arms reuse J's plan
   and metadata), judge the clips blind, and decide defaults.
   Also rerun `benchmark/batch.sh batch1` (or a subset, spaced out) to
   measure B2 fully against B1.
2. YouTube fallback: stays OFF and not functional until the owner decides.
   To validate: owner supplies a throwaway-account cookies.txt outside the
   repo (e.g. ~/.config/scenery-brief-clips/youtube-cookies.txt, chmod 600)
   or a proxy; set it in a NON-committed config; run one blocked request;
   confirm `youtube_fallback_used: true` and no credential in any run file.
   Note: the project config.yaml is not gitignored; never put a proxy URL
   with a password there.
3. Owner decisions open: cookies vs proxy vs waiting; B3
   design; Jev defaults.
4. B3 brief profiles: partly superseded by the unattended track (sources
   scale with n_clips, labels stop at n_clips). Left: letting "uncertain"
   moments fill a shortfall, if the live batch still shows shortfalls; the
   orchestrator questions/profile idea. "european" query fix: dropped
   (place not a priority).
5. Plan step 6 (Hermes skill) on hold; plan step 7 (quality signals)
   deprioritized.
6. Merged into master (2026-09-29). Further merges only when the owner asks.

## Commands

```bash
.venv/bin/scenery-brief-clips doctor
.venv/bin/python -m pytest tests/ -q          # ~6 min; never alongside a benchmark
# before timing anything: uptime; ps aux --sort=-%cpu | head  (leave hermes gateway alone)
benchmark/bench.sh stepN-ocean $PWD/tmp/bench/baseline-20260928T154614Z/data/runs/20260928T154616Z
BENCH_INPUTS=military benchmark/bench.sh stepN-military $(ls -d $PWD/tmp/bench/military-base-*/data/runs/*)
BENCH_INPUTS=step4-reddeer BENCH_LIVE_PLANNER=1 benchmark/bench.sh reddeer   # cold, one command, live
BENCH_INPUTS=heldout-horses BENCH_NO_EXPORT=1 benchmark/bench.sh horses       # live, stops before export
benchmark/batch.sh batch1 [R01 R03 ...]       # sequential live batch -> benchmark/runs/batch1-index-*.tsv
.venv/bin/python benchmark/batch_summary.py benchmark/runs/batch1-index-<utc>.tsv --out summary.json
# is YouTube accepting the host? ONE call, rarely:
yt-dlp --ignore-config --js-runtimes node --skip-download --print id https://www.youtube.com/watch?v=Lc4Bn1v8iZk
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
