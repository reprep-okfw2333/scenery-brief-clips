# Architecture

This is scenery-brief-clips: an independent fork of scenery-clips whose only
architectural addition is a frozen-brief, model-planned discovery stage ahead
of the inherited funnel. Downstream stages (C–F below) are scenery-clips code,
unchanged in behavior.

Goal: from a validated structured brief (or a legacy prompt string), return N
on-disk clips plus a manifest. If fewer than N pass, return what passed and
say why. Do not silently loosen the brief. With no named resolution, the
source-acceptance default is ≥720p (1280×720); an explicit resolution remains a gate.

Honest cost rule: never pull an export rendition until excerpts are chosen. Low-resolution analysis copies and storyboard JPEGs are allowed after cheap gates.

Implementation status of each stage is in docs/ROADMAP.md and docs/STATUS.md.

## Stages

-1. Frozen brief + one-call query planner  (built in this fork; Part 7A/7B)
   A structured `search_brief_v1` JSON is validated by `brief.validate_brief`
   (strict schema: provenance quotes, geometry bounds, limit caps,
   contradiction checks) and hash-frozen via `canonical_json_hash`. The
   planner instruction (`search_query_planner_v1`) is application-owned and
   fixed. `planner.plan_queries` makes exactly ONE model call (wire named in
   planner.yaml, mirroring vision.yaml's no-secrets rules) returning a
   `search_queries_v1` plan, which is validated against the brief (version,
   hash binding, subject fidelity, length/duplicate bounds, strategy set).
   `--plan` skips the model call and validates a frozen plan file instead.
   The planner only proposes search phrases; it never judges pixels, sets
   policy, or downloads. Validation failures and empty plans stop the run
   fail-closed (exit 2 / exit 1 with `empty_query_plan`); there is no
   fallback search that quietly broadens the brief. Provenance (model,
   instruction version, elapsed seconds, plan hash) is recorded without
   secrets. Provenance reality: queries are leads only; every discovery
   result is `visual_status=unverified`, `acceptance_level=metadata_only`.
   Legacy `run --dry-run --prompt` and `build_queries()` are unchanged and
   remain the default for prompt callers. Full contract: docs/SEARCH_BRIEF.md.

0. Prompt + policy  (built)
   Compile the sentence into a Constraint (theme, min width/height, aspect band, N, target duration, geo flag, run limits). `visual_positives` and `visual_negatives` are serialized defaults but no pipeline stage consumes them; they are NOT active label or export gates. The wired vision prompt uses the actual theme text and its conditional activity/scenery rubric.
   Flat project config.yaml or --config can provide stricter/default limits; explicit flags win, and config cannot enable downloads or loosen prompt geometry.
   No named resolution defaults to 1280×720; 720p and 1280×720 mean 1280×720; 1920p means 1920×1080. allow_download stays false until the explicitly authorized Part 5 export path.

A. Discovery  (built)
   YouTube search via yt-dlp (`ytsearchN:`). Text only. Queries come from the
   validated brief plan (new path), or from build_queries() variants (legacy
   prompt path). Dedup by video_id. Cap + sleep.
   run-brief adds a discovery.json sidecar (brief_discovery_v1) binding the
   brief hash, full query plan, planner provenance, attempted queries, counts,
   and stop reason; candidates/rejects in the run dir and sidecar carry
   visual_status=unverified and acceptance_level=metadata_only.

B. Metadata eligibility  (built)
   `yt-dlp -j --skip-download`. Keep if a real format has height >= min_height and aspect in band; the default constraint is ≥720p / 1280×720 when the prompt names no resolution. Explicit prompt resolutions and any stricter config remain gates. Ignore “4K” in titles. Duration is a hint, not a class. Live / upcoming / auth / private are rejects.

C. Storyboard prioritization  (built; vision is external)
   Parse storyboard formats from cached player JSON. Sample tiles; do not download every sheet of an 8-hour film. Save tile JPEGs. Status: promising / uncertain / low / unknown, plus time windows.
   Missing storyboards = unknown. Dark tiles are not keepers.
   Without a scores file, non-dark videos stay uncertain.
   After apply-scores: keep tiles → promising windows; city/people/title tiles drop out of ordinary scenery windows, while visible people/machines doing the requested activity may be kept for an activity brief. Mixed travel films can donate a few nature windows. Tile model calls are I/O-bound and capped at two concurrent calls; score order and per-image failures are retained.

D. Analysis copy + cuts  (built and stabilized)
   For promising/uncertain sources, ≤720p video-only copies of the selected windows. If selected windows exceed the budget, each is capped internally; analysis never falls back into rejected regions. Without windows, short sources use full and long sources use spread sampling.
   Every yt-dlp format fallback is video-only and capped at 720p. All yt-dlp subprocesses use `--ignore-config`, so host configuration cannot silently enable cookies or change download policy.
   Cache identity is video ID + canonical source span + policy version. Per-key locks, unique staging paths, full probe/decode validation, SHA-256 completion markers, and atomic replacement prevent stale/partial reuse.
   Planned spans are processed concurrently across selected videos (default 4 workers via SCENERY_ANALYZE_WORKERS). Output order stays ranked/span-stable. Used by both legacy `run` and `run-brief` outputs.
   PySceneDetect AdaptiveDetector + ThresholdDetector, min_scene_len ≈ 0.5s (detection spacing, not usable duration). start_in_scene=True. Default frame_skip=1 (SCENERY_DETECT_FRAME_SKIP; set 0 for every-frame legacy). Soft dissolves are not trusted to PySceneDetect alone.
   Fetched media must match its planned span within 3s (the same tolerance verify uses); a shorter download fails the range, and stale short cache entries are dropped and re-fetched on re-run instead of being certified complete forever. Constraint duration settings (target/min/max) are validated at analyze time, so analyze can no longer publish excerpts its own verify would reject.
   Shot → trim edges → drop if still < min duration; keep interior if in band; if long, take a target-length excerpt inside.
   Continuity gate (deterministic, fail-closed): ~2 fps dHash + mean-RGB samples on the analysis copy; if adjacent or first↔last distances exceed config thresholds, trim to the longest stable subspan that still meets duration_min_s, or reject the excerpt. Bad windows never enter excerpts.json. Decode defaults to width 160 before feature extract (SCENERY_CONTINUITY_DECODE_WIDTH; 0 = full frame). Other knobs: continuity_enabled, continuity_sample_fps, continuity_max_*_hamming, continuity_max_*_color (see config.example.yaml); recorded in analysis_manifest settings (includes analyze_workers).
   Each range records attempts and outcome. excerpts.json and analysis_manifest.json bind rows to exact ranked/constraint inputs and settings; changed inputs or incomplete work fail closed.
   Generic full-quality download stays forbidden.

E. Shortlist  (built and stabilized)
   Temporal review of each excerpt as a sequence: frames across the moment (not one still; default 6, always including near-start and near-end), saved under the run's review/, joined into a strip per moment, and recorded in review.json bound to the excerpts generation. Moments whose strip endpoints diverge strongly are flagged continuity_suspect (defense in depth; Part 3 already gated continuity). shortlist-apply excludes a suspect keep unless cleared with note prefix continuity_ok: or continuity_ok=true (reason continuity_suspect_uncleared). Vision stays external: Hermes/a person labels moments (match + geo + scene_type + note) into shortlist_scores.json, then shortlist-apply recomputes the decision deterministically into shortlist.json.
   Two fields per moment: visual match to the brief vs geographic evidence (supported / uncertain / conflicting). Conflicting geography is never selected; uncertain geography is selected only with flags ["geo_uncertain"]. Non-keep and unscored moments are excluded with recorded reasons.
   Near-identical moments collapse via per-frame difference hashes (min Hamming distance ≤ 10); remaining picks round-robin across scene_type groups, preferring least-used sources.
   Stops at n_clips or returns a truthful shortfall with per-exclusion reasons. No silent loosening, no padding. Deterministic: identical inputs produce identical bytes.
   verify additionally checks any shortlist.json: bindings to the reviewed generation and labels file, exact re-derivation from its inputs, referenced cache keys, reason coverage, and count reconciliation. A run without a shortlist verifies as before.

F. Acquire + export  (built 2026-09-22; design + rules in docs/EXPORT.md)
   Plan-first: an immutable per-moment plan resolves the LARGEST advertised
   video-only rendition at or under `export_max_height` (default 720p, valid
   integers 720..2160), never below the 720p floor, never scaled up or down,
   native 16:9, and never beyond the recorded source dimensions. The slightly-
   wide acquisition span is written before anything downloads.
   Acquisition is stream copy with copyts so the section keeps ABSOLUTE source
   timestamps; K is the first/minimum packet PTS and equals `format.start_time`,
   the mapping anchor. The moment fails unless the section provably covers the
   exact clip interval. Acceptance: one video stream and no other streams,
   dimension equality, square pixels, no rotation, SDR/8-bit allowlist, full
   decode, and frame count/max-gap checks inside the delivered clip window.
   `probe_export_coverage(path, window_ms=...)` reports `window_n_frames` and
   `window_max_gap_s` for that interval. The half-open window test compares
   full-precision packet PTS against millisecond endpoints: rounding a packet
   at 33.032833s up to 33.033s must not exclude it from an interval ending
   33.033s. Exact expected-frame equality, coverage and max-gap gates remain.
   Fragment-boundary holes in the wider acquisition margins are tolerated when
   they are outside that window; live verification found holes at section tails
   while delivered content stayed continuous. Exactly ONE libx264 encode trims
   the interior on a half-open [start,end) frame grid. Outputs publish transactionally into
   out/<theme>/ (schema-2 plan.json, clips/, schema-2 manifest.json with
   max_height) followed by the schema-2 run-dir pointer; verify proves the
   export against the [720,max_height] band.

## Tool split

  yt-dlp       search, metadata, analysis copies, later HD ranges. Never scores pixels.
  Storyboards  cheap timeline stills, not frame-accurate cuts.
  Planner      ONE text-model call proposing search phrases for a frozen brief.
               Switch in planner.yaml (no secrets). Validated deterministically
               afterward; can be replaced by a frozen --plan file. Not pixels.
  Vision       tile and strip labels. The model is named in vision.yaml, not hardcoded. Hermes must show that model and get a yes before a run. Not cuts.
  PySceneDetect  cut times only, not “European scenery”.
  ffmpeg       analysis decode, later export and probes.

## Boundaries

  Acquire wide, export interior. Do not expand the final clip into neighboring shots.
  Detection spacing ≠ minimum usable duration.
  A planner query is a search lead, not evidence of scene content. Brief scene
  fields and exclusions are recorded context — the visual stages do not
  enforce them; operator review remains mandatory before delivering clips.

## Home

  /root/projects/scenery-brief-clips (this fork; independent git history).
  The inherited original lives at /root/projects/scenery-clips and is treated
  as read-only by this project.
