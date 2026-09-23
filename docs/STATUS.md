# Status (read this first)

Anyone picking this up: this file is the current snapshot. Open problems are in docs/ISSUES.md. Design lives in docs/ARCHITECTURE.md. Implementation order lives in docs/ROADMAP.md. Do not start a new part until the user explicitly says to.

Project root: this repository (discoverable; do not hard-code a machine-specific absolute path as the only instruction).
Do not scatter caches, notes, or clones. Runtime data is gitignored under data/ and tmp/. Host /tmp is a small tmpfs — never put media there.
Expected host: Linux with Python 3.14 (see .python-version). Native Windows lacks fcntl flock used by run/export locks — use WSL or Linux.

## What is built (Parts 1–6 + external vision)

Part 1  Search + metadata gate. No video download.
        CLI: doctor, explain-prompt, run --dry-run
        Query variants preserve earlier hits if a later search fails; cached metadata remains usable after the network budget is exhausted.
Part 2  Storyboard tiles + vision labels.
        CLI: rank (saves tiles), vision-show, label-tiles --confirm-vision, apply-scores
        The model is named in vision.yaml, not hardcoded. label-tiles refuses without --confirm-vision.
        Dark tiles are recorded as reject and are not sent to the model.
Part 3  Capped ≤720p video-only analysis copies (AVC/H.264 preferred) + PySceneDetect + continuity gate + excerpt timestamps.
        CLI: analyze
        Continuity gate (~2 fps dHash + mean-RGB) trims or rejects mid-excerpt dissolves/cuts PySceneDetect misses before excerpts.json is published.
        Every yt-dlp fallback is video-only and ≤720p; inherited yt-dlp config is ignored.
        YtDlp.download() (full quality) is still forbidden.
Part 4  Shortlist: temporal review of candidate moments, dedup, diversity, shortfall.
        CLI: shortlist-review (frames + strips + review.json; default 6 frames, endpoints included), shortlist-apply (labels → shortlist.json)
        Vision can be the wired model (label-strips --confirm-vision) or a person. The wired call writes shortlist_scores.json; shortlist-apply still counts it.
        Labeling rubric: reject strips that show a cut/dissolve mid-moment (defense in depth on top of the Part 3 gate).
Verify  CLI: verify  (input hashes, planned ranges, referenced media, markers, geometry, duration, full decode, and the shortlist when present)
Part 5  Capped export (implemented + live-verified 2026-09-22). Design, decisions, and done criteria: docs/EXPORT.md.
        Guiding facts (proven empirically 2026-09-22): the default export cap
        is 720p, with `export_max_height` configurable from 720 through 2160;
        each final clip is the largest native 16:9 video-only rendition at or
        below that cap, never scaled. With copyts, a section keeps absolute
        source timestamps: K is the first/minimum packet PTS and equals
        `format.start_time`, so source->local mapping is local = source - K;
        coverage (first_pts <= clip start, last frame end >= clip end) is the
        acceptance gate. Stream copy only; ONE libx264 crf17 encode per clip.
        Implementation: export.py + yt.fetch_export (plan-authorized specs) + CLI
        export + verify export checks (--require-export); config allow_export and
        export_max_height gates.
Part 6  Hermes skill at `.hermes/skills/scenery-clips/SKILL.md`. Loads when the
        checkout is trusted (`hermes skills trust`) and a session starts in this
        repo. Small end-to-end: 3 clips on disk with a schema-2 manifest.
        Evidence: data/runs/20260922T160610Z and out/part6-european-scenery-3/.

Tests: 309 passed in 229.37s on the last recorded full-suite run for the
tree-cutting stabilization pass (before the ocean run). The ocean run was live
verified as described below; this documentation update did not rerun tests.
Python 3.14, uv project, deps in .venv (pillow, scenedetect, opencv).
System binaries: yt-dlp, ffmpeg, ffprobe.
Pytest temps are forced onto project tmp/ (tests/conftest.py). Host /tmp is a
small tmpfs; export's 2GB free-disk guard is real and must not be lowered to
make those tests pass.

Vision switch: vision.yaml in the project root. The setting approved for the
latest live test is codex-login / gpt-6-sol (ChatGPT sign-in, not a paid key).
Change backend and model there to point at another API later. Do not put a key
in the file. `vision-show` prints the current model. label-tiles and
label-strips refuse without `--confirm-vision`. Hermes must show the model
and get a yes before a live picture-labeling run. An earlier castle-tile test
through the prior gpt-6-astra wire returned reject / not_nature.

AMENDED 2026-09-22 — Part 5 contract and source-floor decision:
  The Astra consult review replaced the earlier “source full resolution, never
  below 1080p” export contract with a 720p cap. Export now delivers the largest
  video-only rendition at or under `export_max_height` (default 720p; valid
  720..2160), with a constant 720p floor, native 16:9, and no scaling.
  Prompts that name a resolution still gate to it (1920p → 1920×1080; 720p and
  1280×720 → 1280×720); prompts with no resolution now default to 1280×720.
  Aspect band, live/upcoming/auth filters, and storyboard behavior are unchanged.
  Mapping evidence is empirical for both avc1 and av01 720p renditions: on the
  same source window, copyts produced the identical K = 966598 ms (the
  first/minimum packet PTS, also the container start_time); frames at absolute
  source times 970.0/972.5/975.0 s had dHash distances 2/1/0. The historical
  run `data/runs/20260918T210550Z` keeps its recorded 1920×1080 constraint and
  4K sources; 720p clips still verify against it because final clips use the
  export cap band, not the recorded constraint floor.

## Parts 1–3 stabilization — complete

Completed and regression-tested:
  1. Every yt-dlp call ignores host config; every analysis fallback is video-only and ≤720p.
  2. Analysis cache keys include canonical source start/end milliseconds and policy version. Legacy index-only entries are never reused.
  3. Downloads use per-key locks, unique project-local staging files, validation, SHA-256 completion markers, and atomic publication.
  4. Every planned range records complete/failed outcomes and all attempt errors. Partial/failed/invalid analysis exits nonzero.
  5. excerpts.json is bound to the exact ranked.json, constraint.json, settings, and generation hash. Mid-run input changes abort publication.
  6. verify recomputes plans and checks only run-referenced media: cache identity, marker, hash, size, one video/no audio, ≤720p, aspect, duration, and strict decode.
  7. Scored windows remain authoritative. If they exceed the analysis budget, each selected window is capped internally instead of falling back outside vision-selected regions.
  8. Flat project config is supported through project config.yaml or --config; CLI flags override config, and config cannot enable downloading or loosen prompt geometry.
  9. A later query failure keeps exit-code precedence even when the metadata budget is also exhausted; both conditions stay in the run log.
  10. Vision score files are schema-validated (object shape, known labels, finite timestamps) before ranked.json is touched; invalid files exit 2 with no changes.

Live evidence for Parts 1–3 appears in the historical runs below; the newest
delivery is the ocean run near the end of this file. Part 5 (capped export) was
explicitly authorized and built 2026-09-22; a new part still needs explicit
user instruction.

`uv` is often missing from PATH on this host. Use:

  cd <project-root>
  .venv/bin/scenery-clips doctor
  .venv/bin/python -m pytest tests/ -q
  .venv/bin/python scripts/chain_parts123.py

## Earlier European live run (historical, not the latest run)

  data/runs/20260918T210550Z
  Prompt: Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips
  Constraint compiled to 1920×1080, aspect 1.70–1.86, n_clips=20.

  5 candidates, all 3840×2160 16:9, 0 metadata rejects.
  After vision apply-scores:
    4 promising (56cUon-VlBQ, CVcVG9Q2z0Q, dM0xOIWtMv8, nRdZPHLC4s4)
    1 uncertain (0xhzwDXfLds — city tiles rejected; middle tiles never labeled)
  Post-vision analyze:
    5/5 rows complete, 0 range errors, 0 recovered attempt errors.
    12 excerpts backed by 12 distinct span-keyed ≤720p video-only files.
    verify: ok=true, ready_for_shortlist=true, full probe + decode passed.
    An unchanged replay reused all 12 cache entries without changing their hashes.
    A stale ranked.json smoke was rejected; an unrelated empty shared-cache file was ignored.
  Post-vision shortlist (Part 4):
    12 moments reviewed from frames + strips (4 frames each, source-time stamped);
    labels in shortlist_scores.json (match + geo + scene_type + note per moment).
    8 selected, 4 excluded — 2 rejected by temporal review (city aerial with a cut;
    "EUROPE" title overlay), 2 uncertain match (clip contains a hard cut mid-moment).
    4 of the 8 picks carry flags: ["geo_uncertain"]; 4 are geo supported.
    shortfall: 12 of 20 requested, explained per exclusion; nothing was padded.
    verify: ok=true including shortlist checks; re-apply is byte-identical
    (sha256 baf1ed4a... — fix 3 made labels_path relative to the run dir, so the
    value moved from the pre-fix file; consecutive re-applies produce equal bytes).
    Duplicate-collapse demonstration lives at data/runs/dupdemo-demo (fixture run:
    reused footage collapsed to one pick, "duplicate of demo0000001:0", verify ok).

  Part 5 export (2026-09-22, amended 720p-cap contract):
    plan: all 8 selected moments resolve to native 1280x720 video-only renditions
    (avc1 136; one 720p60 298 via the fps tie-break); no 4K downloads, no scaling.
    live pass: 8/8 exported, 0 failed — out/beautiful-natural-european-scenery/clips/
    (1280x720, 16:9, 180/342/359 frames, ~40 MB total; every clip probed + decoded).
    First pass exposed a real-world artifact: the section downloader leaves
    single-frame holes at fragment boundaries in the acquisition MARGIN; the
    acceptance contract now enforces frame count + max gap on the CLIP WINDOW only
    (a unit test reproduces the exact production message; margin holes are
    tolerated, content verified continuous across them).
    verify: --require-export ok=true, 0 errors, exported=8, failed=0,
    export_complete=true, acquisitions_checked=8 (pointer -> manifest -> plan ->
    clip hashes -> rich probe -> acquisition-cache binding).
    idempotent re-run: manifest bytes identical (sha256 979150bd...), 11 s, zero
    re-encodes — every clip and acquisition reused from validated caches.
    copyts mapping: identical K=966598 ms across avc1/av01 720p renditions of the
    same window; cross-codec frames at absolute 970.0/972.5/975.0 s match (dHash 2/1/0).
    Two live findings folded back into verify: a leaked loop variable made clip
    checks compare against the wrong plan moment (regression test added — red
    against the old code, two-moment fixture), and manifest durations needed the
    same +/-50 ms tolerance as the shared validator (29.97 fps 6 s = 6006 ms).

  ranked_before_vision.json  backup of pre-score labels
  vision_scores.json         tile labels from Hermes vision_analyze (not every tile)
  shortlist_scores.json      per-moment shortlist labels from the temporal review
  review.json                frame/strip packet bound to the excerpts generation
  analysis_manifest.json     hashes ranked/constraint/excerpts + effective analysis settings
  shortlist.json             selected + excluded moments, reasons, counts, shortfall

scripts/chain_parts123.py runs parts 1–3 and verify. It does not rank-save-tiles-for-vision as a separate scored step and does not call apply-scores.

## What is NOT built (do not pretend it is)

Nothing in Parts 1–6 of the inherited pipeline. Part 7A/7B/7C (frozen brief +
bounded planner + run-brief) IS implemented in this fork — see its section
below. A new part still needs an explicit user start and written done criteria.

Part 5 — capped export: DONE (see evidence above).
Part 6 — skill + 3-clip delivery: DONE (see Part 6 evidence below).

Intentional boundaries / later work:
  Vision remains external to the Python process: rank saves tiles and shortlist-review saves frames, a person/Hermes labels both, then apply-scores / shortlist-apply import the labels.
  Geographic evidence is recorded per moment in shortlist.json (supported / uncertain / conflicting) and never upgraded silently.
  Cover thumbnails are deliberately not used for timeline scoring; missing storyboards remain unknown rather than trusting one promotional image.

## Continuity hardening (2026-09-22)

Smoke export `data/runs/20260922T140101Z` clip1-fjord (`0xhzwDXfLds` e0) shipped a soft dissolve in the last ~1s of a 6s clip: PySceneDetect returned one ~14s scene, the interior 6s excerpt straddled the dissolve, and shortlist-review's old `(i+1)/(n+1)` sampler never hit the endpoints (4 frames over 6s left the last 1.2s blind; dissolve started ~0.07s after the last sample). Still vision correctly kept what it saw; continuity was never checked.

Landed:
  1. Part 3 continuity gate on candidate excerpts (config knobs in config.example.yaml; defaults on): sample ~2 fps from the ≤720p analysis copy; if max adjacent or first↔last dHash Hamming / mean-RGB distance exceeds thresholds, trim to the longest stable subspan that still meets duration_min_s, else reject. Fail-closed; recorded on excerpts / continuity_rejected / analysis_manifest settings.
  2. Part 4 sample_times always includes near-start and near-end (50ms inset); default frames 4→6. review.json may set continuity_suspect when strip endpoints diverge.
  3. SHORTLIST labeling rubric: reject strips with mid-moment cut/dissolve; vision is defense-in-depth.
  4. Fixture tests under tests/fixtures/continuity_*.mp4; pure unit tests for trim/reject decisions.

Re-run shortlist-review on an old run (endpoints + suspect flags only; does not rewrite excerpts):
  .venv/bin/scenery-clips shortlist-review --run-dir data/runs/20260922T140101Z
To rebuild excerpts through the gate, re-run analyze on that run (cache hits expected), then shortlist-review / shortlist-apply / export as usual.

## Review fixes (open findings)

Independent review (deepseek-flash, 2026-09-21) of Parts 1–4 confirmed these; fixing one at a time with tests:
  1. Malformed run files crashed shortlist-apply/shortlist-review with tracebacks or produced silent empty packets — FIXED: shared byte-read + JSON loaders raise clean ShortlistStaleError / ShortlistInputError for constraint, ranked, excerpts, manifest, and review files; non-object rows/excerpts and non-list excerpt fields are rejected; malformed frame_hashes are rejected; build-stage errors are attributed to "build" (was always "extract"); corrupt manifests on shortlist-review exit 1 (was 2). 21 regression tests. verify already handled a corrupt constraint cleanly.
  2. review.py built moment paths from the unsanitized video_id in excerpts.json (a crafted id could escape the run dir and delete/overwrite outside it) — FIXED: folders now use analysis_cache.safe_video_id with an explicit containment check, frame names are microsecond-stamped and collision-checked, and all six run-scoped CLI commands (rank, analyze, apply-scores, verify, shortlist-review, shortlist-apply) refuse a missing --run-dir with exit 2 and create nothing. 8 regression tests; the crafted-id repro is contained.
  3. verify rejected legitimate shortlists when a duplicate's winner lost the quota race ("duplicate of X" where X was excluded "beyond n_clips"), and two bindings were loose — FIXED: the checker now accepts a referenced winner that is itself excluded (references are resolved against the analyzed moments instead of string-splitting, and unknown/self/chained references still fail), review.json must match the analysis generation, and the labels file must stay inside the run dir (recorded relative to it; the live run's absolute in-run path still verifies). 6 regression tests.
  4. Part 3: media shorter than the planned span was published as complete with a valid marker (analyze exited 0; the cache then stuck), inverted/out-of-band excerpts could be certified from inconsistent duration settings, and a total metadata failure exited 0 — FIXED: fetch-time validation enforces duration-vs-span (same 3s tolerance as verify) and rejects + replaces stale short cache entries on re-run (healing); constraint duration settings are validated fail-closed (analyze exits 2 without writing); excerpts_from_scenes validates its inputs and outputs; a run where every metadata fetch failed reports stopped_reason `metadata_errors` and exits 1. 8 regression tests.
  Polish items (fixed): n_clips must be a real integer (2.9/2.0/true/"20" exit 2); the labels file may carry an optional top-level excerpts_sha256 that shortlist-apply and verify enforce when present (mismatch = "different analysis generation"); empty frame_hashes fail closed as stale (dedup can no longer be silently disabled for a moment); ranked.json and ranked_before_vision.json are written atomically via store.write_json_atomic (temp + fsync + rename); abandoned *.staging-* files older than 1h are swept at fetch time (lock files are intentionally kept — unlinking a flock target can break mutual exclusion); doctor checks node; apply-scores reports unmatched_score_ids (stderr warning + payload field) instead of silently ignoring them. 9 regression tests.
  Exit-code rationale kept: verify/analyze exit codes report integrity/completion, not "readiness to shortlist" — a completed source with no usable cuts stays exit 0 and is disclosed via videos_without_excerpts + ready_for_shortlist (docs/CLI.md); gate automation on ready_for_shortlist.


## Change/tighten pass (2026-09-22)

  1. `continuity_suspect` now affects shortlist-apply: a suspect moment labeled
     `match=keep` without clearance is excluded as `continuity_suspect_uncleared`
     (mandatory review, not an auto-delete of pans). Clear with note prefix
     `continuity_ok:` or `"continuity_ok": true`. Documented in docs/SHORTLIST.md.
  2. Export honors approved shortlist `start_s`/`end_s` exactly — no continuity
     re-trim at export. Documented in docs/EXPORT.md.
  3. Shortfall UX: shortlist counts include `request_fulfilled` and upstream
     Part 3 `n_continuity_rejected_upstream` (+ by-reason); explanation and CLI
     summary surface them. No auto-widen.
  4. `doctor` reports python/yt-dlp/ffmpeg/ffprobe/node versions and actionable
     messages (yt-dlp too old / node missing for JS runtimes). yt-dlp retries
     classify transient vs fatal with a small attempt budget; stderr preserved.
  5. Docs/setup: Part 5 marked implemented (not "in progress"); project root
     described as discoverable; Linux/Python 3.14 + Windows fcntl note; default
     shortlist-review frames remain 6.

## Part 6 evidence (2026-09-22)

  Skill: `.hermes/skills/scenery-clips/SKILL.md`.
  `hermes skills trust` recorded this checkout. A fresh process with this
  repo as cwd listed that skill file and did not quarantine it. This
  session's own skill index was built before the file existed, so it does
  not show here until a new session starts in the repo.

  Run: data/runs/20260922T160610Z
  Prompt: Beautiful natural european scenery, 720p, 16:9, 3 individual clips
  Constraint: 1280×720, aspect 1.70–1.86, n_clips=3. Downloads stayed off
  until export was explicitly allowed.

  Search: 6 candidates kept, 1 rejected (live), stopped_reason complete.
  Ranked 4 films, 6 tiles each. After tile labels: 3 promising
  (0xhzwDXfLds, CVcVG9Q2z0Q, BTMjD7_evjE), 1 low (OcTvr9dys_8, castles and
  title text). unmatched_score_ids empty.
  Analyze: 3 complete, 1 skipped, 0 range errors, 6 excerpts. One upstream
  continuity reject (0xhzwDXfLds river-valley window, stable span 3.218s
  below the 4s minimum). verify: ok=true, ready_for_shortlist=true.
  Shortlist: 6 moments reviewed (6 frames each). 3 selected, 3 excluded
  (beyond n_clips x2, visual match rejected x1 — a town filled the frame).
  shortfall 0, request_fulfilled true. No continuity_suspect flags.
  Selected moments: 0xhzwDXfLds e0 (fjord, geo uncertain), BTMjD7_evjE e0
  (alpine peak), BTMjD7_evjE e2 (alpine reservoir).

  Export refused the default theme folder because out/beautiful-natural-european-scenery
  is owned by 20260918T210550Z. Re-ran with --theme part6-european-scenery-3.
  That older folder was not modified.
  live pass: 3/3 exported, 0 failed. Clips are 1280×720, format 136, sizes
  match the manifest (145 frames / 4838 ms; 180 frames / 6006 ms; 180 frames / 6006 ms).
  verify --require-export: ok=true, 0 errors, exported=3, failed=0,
  export_complete=true, request_fulfilled=true, acquisitions_checked=3.
  Pointer: data/runs/20260922T160610Z/export.json schema 2, manifest sha256
  d4a6a0d0a050aed149ff515986afdca7c3437f2efdbbbbec6642393c5ad4afce.
  Files: out/part6-european-scenery-3/clips/ and manifest.json.

## Earlier tree-cutting run (historical failure before this pass)

  Run: data/runs/20260922T183756Z
  Prompt: Trees being cut down, 720p, 16:9, 4 individual clips
  Vision then: vision.yaml codex-login / gpt-6-astra, after the user asked for that run.
  Search: 7 kept, 1 rejected (no_min_resolution). Ranked 6. All 6 became promising after tile labels.
  Analyze: 6 complete, 0 range errors, 6 excerpts, 1 source with no usable cut (REv14e4G_dQ).
  At the time, verify reported ok=true, ready_for_shortlist=false because of that empty source. With the current verifier on unchanged old data: ok=true, ready_for_shortlist=true, n_sources_without_excerpts=1.
  Shortlist: 4 selected, 2 excluded (title overlay; people talking beside already-cut logs). 4 upstream continuity rejects never became candidates.
  Export --theme trees-being-cut-down: 3 exported, 1 failed at the time.
  Failure then: hkBLufo-ZS4 e0, timestamps_invalid, 131 rounded frames in the window, expected 132, twice. A new section repro and the current probe establish 132 real packets inside the window; the old export manifest was not rewritten.
  Clips on disk: out/trees-being-cut-down/clips/ (3 historical files). Details: docs/ISSUES.md.

## Earlier stabilization run (2026-09-22) — verified partial fulfillment

  Run: data/runs/20260922T201824Z
  Prompt: 4 short clips of trees being cut down. Compiled to theme `trees being cut down`,
  1280×720 source floor, 16:9 band, n_clips=4. Wired and user-approved vision:
  codex-login / gpt-6-sol. Final-section export explicitly authorized for this test.
  Search: 7 candidates, 1 resolution rejection; ranked 6 sources, 24 tiles.
  Tile labels: 20 model calls (4 dark skips), 3 keep / 18 reject / 3 uncertain;
  apply-scores produced 2 promising, 2 uncertain, 2 low.
  Analyze: 4 complete, 2 skipped, 0 range errors, 2 excerpts, 2 complete
  sources with no usable excerpts; 2 continuity candidates rejected upstream.
  verify before review: ok=true, ready_for_shortlist=true, n_excerpts=2,
  all_analyzed_sources_have_excerpts=false, n_sources_without_excerpts=2.
  Review: 2 moments / 12 frames, no extraction failures. Both strips were labeled
  keep for actual tree-cutting activity, with geo_uncertain rather than an
  unsupported geographic claim; 2 selected, 0 excluded, shortfall 2.
  Export: out/stabilize-tree-cutting-20260922-201824/ — only the two selected
  sections downloaded; 2 exported, 0 failed, export_complete=true,
  request_fulfilled=false. The prior out/trees-being-cut-down/ remains untouched.
  verify --require-export: ok=true, errors=[], exported=2, failed=0,
  acquisitions_checked=2. An independent manifest/plan/shortlist/file check
  confirmed the exact two clip paths, hashes and sizes, one 1280×720 video-only
  stream each, 250 and 465 frames, and complete error-free ffmpeg decode:
    out/stabilize-tree-cutting-20260922-201824/clips/cm6W5OFBV84_e0_538676-547033.mp4
    out/stabilize-tree-cutting-20260922-201824/clips/yRELcUn2ZnE_e0_280551-288308.mp4
  Measured stage wall seconds: search 23.74; rank 1.89; tile labels 64.71;
  apply 0.36; analysis 86.82; pre-review verify 16.34; review 11.01;
  strip labels 8.48; shortlist apply 1.07; export 69.19; final verify 20.84.
  Sum of these stages 304.45s. Run creation to manifest publication was 433.87s
  with pauses for concurrent documentation/investigation. The old ~25-minute
  run had more excerpts/exports, an interrupted analysis, and a different model;
  its total is not a controlled comparison. The same 20-tile dry-call benchmark
  improved from 2.409s sequential to 1.216s with two bounded calls.
  Root-cause repro of the old 131/132 refusal: a fresh, project-local download
  of its exact section has 132 raw-PTS frames and max clip-window gap 0.033367s;
  full validation now passes without accepting a missing frame or changing endpoints.
  Remaining: request shortfall 2 (do not pad), variable live latency, duplicate
  analysis/export acquisition, no durable model identity in raw vision_scores,
  and unused visual_positives/visual_negatives fields. Details: docs/ISSUES.md.

## Latest delivery: ocean waves (run 20260922T205123Z; completed 2026-09-23 UTC)

  Run: data/runs/20260922T205123Z
  Brief: four separate short 16:9 720p clips of ocean waves against rocky
  cliffs, with no people. The user authorized gpt-6-sol via the ChatGPT sign-in
  for image labels and explicitly allowed export of the selected sections.
  Search: 15 eligible sources, 4 metadata rejects. Ranked and vision-labeled
  8 sources; all 8 promising after tile scores. Analyze produced 68 usable
  moments across 7 sources; 1 source had none, and 5 additional candidate
  moments failed the upstream continuity gate.
  Review: 68 strips / 408 frames, 0 recorded errors. Shortlist selected 4
  moments from 4 sources and excluded 64 (51 duplicates, 9 over quota,
  4 visual rejects). Shortfall 0; request_fulfilled=true.
  Export: 4 selected / 4 exported / 0 failed; export_complete=true. Files:
    out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/97bDawihFDc_e1_328542-337942.mp4
    out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/Oh_Kx01eUV4_e0_338465-349865.mp4
    out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/bAtvCJmwzMg_e0_328449-337849.mp4
    out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/m3v9Rj7-QAk_e0_843-11152.mp4
  Manifest: out/ocean-waves-crashing-against-rocky-cliffs-no-people/manifest.json
  The run agent reported verify --require-export ok=true and an independent
  ffprobe/frame spot-check. During this docs update, the four on-disk files,
  sizes, hashes, and manifest-pointer hash were read back and matched; the
  full verifier and test suite were not re-run.
  Operational lessons: analyze and shortlist-review were first launched in
  the foreground and hit tool timeouts; the later tracked background jobs
  completed. Vision-label commands have no incremental output. The report's
  original 79-moment / 56-duplicate totals and ~30-minute analysis label were
  corrected against run artifacts and its own 21:04–22:17 interval. The
  overnight pause makes this unsuitable as an active-runtime benchmark.
  Full account and remaining limits: docs/RUN-20260922T205123Z-ocean-waves.md.

## Part 7A/7B/7C: frozen brief + bounded planner + run-brief CLI (implemented)

The discovery stage of docs/SEARCH_BRIEF.md is implemented; vision, analysis,
shortlist, and export are unchanged, and explicit brief exclusions are still
recorded context, not enforced visual gates.

- `src/scenery_brief_clips/brief.py`: strict `search_brief_v1` validation
  (`validate_brief`, `BriefValidationError`), canonical JSON SHA-256
  (`canonical_json_hash`), the fixed `search_query_planner_v1` instruction
  (`PLANNER_INSTRUCTION`), `render_planner`, and `validate_query_plan`
  (`search_queries_v1`, 0 or 2-4 subject-faithful queries). Ported from the
  validated lab pilot.
- `src/scenery_brief_clips/planner.py`: `planner.yaml` wire (backend
  `codex-login` or `openai-api`, model name, no secrets) parsed like
  vision.yaml; `plan_queries(brief, wire, caller=None)` makes exactly one
  bounded model call (instruction as instructions, compact search view as the
  user message), parses JSON, validates the plan against the brief, and
  returns `(plan, provenance)` with schema version, instruction version,
  backend/model, elapsed seconds, and plan hash — no secrets. `queries=[]` is
  a valid refusal-shaped plan. Every failure raises `PlannerError`.
- `pipeline.run_dry` gained `queries=None`; when a list is passed it is used
  verbatim (no re-sort/dedupe). Legacy `build_queries` behavior unchanged.
- `cli.py run-brief --brief <path> --dry-run [--plan <path>] [--planner-config
  <path>] [--max-results] [--max-metadata] [--sleep]`: validates the brief
  (exit 2 with the error code on failure), uses a frozen plan file or makes
  the one planner call (provenance printed as JSON), stops with exit 1 and
  `{"stopped_reason": "empty_query_plan"}` without calling yt-dlp when the
  plan is empty, else runs the budgeted metadata-only search via
  `run_dry(..., queries=...)` with `allow_download=False` and `write_run`s
  plus a `discovery.json` sidecar (`brief_discovery_v1`: brief hash, full
  plan, provenance, attempted queries, counts, stop reason; every candidate
  and reject carries `visual_status=unverified` and
  `acceptance_level=metadata_only`). `--max-results/--max-metadata/--sleep`
  only tighten the brief's own limits (final = min). The new path never
  fetches video, calls vision, or exports.
- `planner.yaml` at the project root selects the planner model
  (`codex-login`, model `gpt-6-sol`), mirroring vision.yaml's no-secrets rule.
- Tests: `tests/test_brief.py`, `tests/test_planner.py`,
  `tests/test_run_brief_cli.py` (offline fakes only; no live YouTube, no
  network model call). Full suite: 341 passed. `explain-prompt` and the
  legacy `run --dry-run` behavior are unchanged.

## Live smoke evidence (2026-09-23, offline-with-frozen-plan)

  CLI: run-brief --brief <lab alpaca brief> --dry-run --plan <lab frozen
  model-query-plan-luna.json> --max-results 6 --max-metadata 6 --sleep 0
  Result: exit 0; run data/runs/20260923T211413Z; 3 planned queries executed
  verbatim; 6 distinct ids; 6 candidates; 0 rejects; stopped_reason complete;
  discovery.json carries brief_discovery_v1 with the correct brief hash
  (8c75b061…), frozen-plan provenance and plan hash; every candidate marked
  visual_status=unverified / acceptance_level=metadata_only; no download
  section flag in the run log (metadata-only confirmed).
  The planner-call path (no --plan) is covered by offline fakes; it has not
  been exercised against the live model from this project yet.

## Known gaps and honest limits (this project)

  1. The planner shapes search queries only. It cannot make rank/analyze/
     shortlist enforce brief scene fields; explicit exclusions stay recorded
     context, not gates.
  2. Search hits are leads: visual_status=unverified always. Downstream
     visual acceptance is inherited from scenery-clips and equally permissive
     (generic "water"/"coast" tile prompts etc.).
  3. Benchmark context (lab, 2026-09-23): on an easy request both the legacy
     query builder and the frozen-brief planner delivered 3/3 usable clips
     (~19 min common stages each); on a hard request (alpacas in a field)
     both produced zero strictly usable clips. Single passes; no general
     performance claim; network bytes and billing were not measured.
  4. The final verifier establishes file integrity, not scene fidelity.
     Operator review of exported clips remains mandatory before delivery.

## Proposed next part (design only)

The implemented Part 7A/7B/7C above covers brief validation, the bounded
planner, and the run-brief discovery path (see the implemented section for
evidence). Still design-only: a live planner-model run from this project, and
any future work making brief exclusions enforceable by the vision stages.
docs/SEARCH_BRIEF.md keeps the full contract; obtain agreement before coding
more.

## Suggested pickup order

1. Read this file, then docs/ISSUES.md, docs/ARCHITECTURE.md, docs/ROADMAP.md, docs/VISION.md.
2. Do not start a new part until the user writes done criteria and says to start. The open problems in ISSUES.md come before new features.
