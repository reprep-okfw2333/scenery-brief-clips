# CLI

From the project root. Prefer the venv binary (`uv` is often not on PATH):

  .venv/bin/scenery-clips vision-show
  .venv/bin/scenery-clips label-tiles --run-dir data/runs/<id> --confirm-vision
  .venv/bin/scenery-clips label-strips --run-dir data/runs/<id> --confirm-vision
  .venv/bin/scenery-clips doctor
  .venv/bin/scenery-clips explain-prompt "PROMPT"
  .venv/bin/scenery-clips run --dry-run --prompt "PROMPT" [--max-results N] [--max-metadata N] [--sleep S] [--config PATH]
  .venv/bin/scenery-clips rank --run-dir data/runs/<id> [--max-videos N] [--max-tiles N] [--config PATH]
  .venv/bin/scenery-clips apply-scores --run-dir data/runs/<id> --scores data/runs/<id>/vision_scores.json
  .venv/bin/scenery-clips analyze --run-dir data/runs/<id> [--max-videos N] [--max-analysis-s S] [--config PATH]
  .venv/bin/scenery-clips shortlist-review --run-dir data/runs/<id> [--frames N]
  .venv/bin/scenery-clips shortlist-apply --run-dir data/runs/<id> --scores data/runs/<id>/shortlist_scores.json
  .venv/bin/scenery-clips export --run-dir data/runs/<id> [--theme SLUG] [--allow-export] [--config PATH]
  .venv/bin/scenery-clips verify --run-dir data/runs/<id> [--require-export]
  .venv/bin/python scripts/chain_parts123.py

vision.yaml is the switch for which model looks at pictures. See docs/VISION.md. Hermes must show `vision-show` and get a yes before starting a run. label-tiles and label-strips refuse without `--confirm-vision`.
When Hermes drives a live run, start label-tiles, analyze, shortlist-review,
label-strips, and export as tracked background terminal jobs with completion
notification (see `.hermes/skills/scenery-clips/SKILL.md`). The label commands
emit no per-image progress; wait for their exit status and published score
file before applying labels. A foreground tool timeout does not prove the
underlying CLI stopped, so do not launch a duplicate without checking.

`--root` on run/rank/analyze/verify/apply-scores/shortlist-review/shortlist-apply/export/vision-show/label-tiles/label-strips sets the project root (default: install location).
Run-scoped commands (rank, analyze, apply-scores, verify, shortlist-review, shortlist-apply, export) require an existing run dir; a missing `--run-dir` exits 2 with "run dir not found" and creates nothing.
run/rank/analyze/export load `<root>/config.yaml` when present; `--config PATH` selects another flat YAML file inside the project. Flags override config. Config cannot loosen prompt geometry, and downloads stay forbidden in the default pipeline (only the export path is gated open, explicitly). `export_max_height` is the export cap: default 720, valid integer range 720..2160.

doctor
  yt-dlp, ffmpeg, ffprobe, node, pillow, scenedetect, tmp/, data/, plus versions (python, yt-dlp, ffmpeg/ffprobe, node) and actionable messages if yt-dlp predates the 2025.11.12 external JS runtime transition or node is missing. allow_download is always false.

explain-prompt
  Constraint JSON. No resolution → 1280×720. 720p or 1280×720 → 1280×720. 1920p → 1920×1080. 4k → 3840×2160.

run --dry-run
  Required flag. Multi-query search + metadata only. Earlier hits survive a later query error; the command exits 1 to disclose that partial search, or a total metadata failure (stopped_reason `metadata_errors`).

rank
  Storyboard sample, save tiles under data/cache/tiles/, write ranked.json (uncertain until scored).

apply-scores
  Relabel ranked.json from vision_scores.json. Writes ranked_before_vision.json once. See docs/VISION.md. Invalid score files (wrong shape, unknown labels, non-finite timestamps) exit 2 without touching ranked.json. Score ids that match no ranked row are reported as unmatched_score_ids (stderr warning + payload field) instead of being silently ignored.

analyze
  promising/uncertain rows only. Selected-window ≤720p video-only ranges, validated span-keyed cache, PySceneDetect, continuity gate (dHash + mean-RGB trim/reject), excerpts.json + analysis_manifest.json.
  Defaults: --max-videos 1, --max-analysis-s 120 unless config overrides. Partial/failed/invalid work exits 1; complete sources with no usable cuts are counted explicitly.
  Spans across selected videos run concurrently (default 4 workers). Scene detection defaults to frame_skip=1; continuity decode width defaults to 160px before dHash/mean-RGB. Applies to runs from both legacy `run` and `run-brief`. Env overrides restore legacy serial/full-frame behaviour:
    SCENERY_ANALYZE_WORKERS=1 SCENERY_DETECT_FRAME_SKIP=0 SCENERY_CONTINUITY_DECODE_WIDTH=0
  Optional phase timings: SCENERY_ANALYZE_PROFILE=/path/to.json.
  Invalid duration settings in constraint.json (non-positive, min > max, or target outside the band) exit 2 without writing. Downloaded media shorter than its planned span fails the range (fetch-time duration-vs-span check, same 3s tolerance verify uses) and stale short cache entries are replaced on re-run instead of being reused forever.
  A completed source with no usable cuts stays exit 0 and is disclosed via videos_without_excerpts. verify.ready_for_shortlist means at least one verified excerpt exists and there are no integrity errors; verify.n_sources_without_excerpts and all_analyzed_sources_have_excerpts disclose per-source completeness separately. A run with no excerpts remains not ready.

verify
  Recomputes the plan and requires exact ranked→row→range→copy completeness.
  Checks ranked/constraint/excerpts generation hashes, cache keys and completion markers, only referenced paths, hashes/sizes, one video stream/no audio, positive geometry, aspect, ≤720p, duration tolerance, excerpt containment, and strict decode.
  When shortlist.json exists it also checks the shortlist bindings, re-derives the decision from (excerpts, review, labels, n_clips), and fails on stale/tampered/inconsistent shortlists. Labels must resolve inside the run dir and review.json must match the analysis generation; a duplicate reference may name a winner that was itself excluded by the quota.
  When run_dir/export.json exists (a published Part 5 export), it schema-validates the pointer, manifest, and plan; reconciles plan.json with the shortlist re-derived above and checks their shared policy/recipe, hashes, generation, and `max_height`. Every clip's dimensions, format id, and acquisition span must match plan.json. Clips are accepted only in the cap band [720, max_height] — the recorded constraint floor is not applied to final clips.
  The rich export probe checks exactly one video stream and no other streams, native 16:9 aspect, frame count and max gap inside the delivered clip window, duration, and timeline origin; clean decode is also required. `probe_export_coverage(path, window_ms=...)` supplies `window_n_frames` and `window_max_gap_s` for that interval. Fragment-boundary holes in the wider acquisition margins are tolerated when outside the clip window; live verification found section-tail holes outside the clip window while delivered content remained continuous. When an acquisition cache entry is present, verify checks its hash, mapping K, and dimensions. Every file in clips/ is inspected: a `*.staging-*` file means publication is in progress or was interrupted; any other unlisted file is an error. The export summary reports `acquisitions_checked`. A changed shortlist, manifest, plan, or clip fails closed with a re-export reason. `--require-export` makes a missing export an error; without it, runs that never exported verify exactly as before.
  Stale or incomplete runs exit 1; unrelated shared-cache files are ignored.

shortlist-review
  Frames sampled across every candidate moment of a verified run (default --frames 6, minimum 2; always includes near-start and near-end), saved under the run's review/ with a per-moment strip; review.json records timestamps, paths, frame signatures, and optional continuity_suspect bound to the excerpts generation. Vision stays external: label the frames/strips, then use shortlist-apply.
  Per-moment extraction errors are recorded; any failure exits 1.

shortlist-apply
  Validates shortlist_scores.json (match + geo + scene_type per moment), applies dedup + diversity rules, and writes deterministic shortlist.json (selected, excluded+reasons, counts, shortfall explanation). Malformed labels exit 2 without writing. Stale run or review material exits 1. Unreadable or malformed run files (constraint/ranked/excerpts/manifest/review) exit 1 with a clear message, never a traceback. The labels file must live inside the run dir; paths outside it exit 2. See docs/SHORTLIST.md.

export
  The ONLY path allowed to acquire export sections, and only for the shortlist's selected moments. Requires explicit CLI export authorization or `allow_export: true` in config, otherwise exit 2 with nothing written or downloaded.
  Reads shortlist.json (bindings re-checked against the analysis manifest, the reviewed material, and the labels file; duplicate or selected/excluded-overlapping identities refused), writes an immutable schema-2 plan (out/<theme>/plan.json — theme = slug of the constraint's theme_text, or --theme), then per moment resolves the LARGEST advertised video-only rendition at or under `export_max_height` (default 720p; valid integers 720..2160; floor 720). Candidates require a native 16:9 aspect within the absolute 0.01 ratio tolerance, strict integer dimensions, and explicit `acodec == "none"`; tie-break order is avc1 > av01 > vp09 > vp9, then higher fps, then ascending format id. No scaling up or down and no cross-resolution fallback.
  It acquires the planned section (yt-dlp --download-sections with a 2s/side margin clamped to >=0 and to the source duration, pinned format id, stream copy with `--downloader-args ffmpeg:-copyts`, --ignore-config, --max-filesize bound) and trims the interior with ONE controlled libx264 encode (crf 17, preset medium, video-only, half-open [start,end)). K is the first/minimum packet PTS; copyts keeps that absolute source time and the container `start_time` equals K, so local = source - K. Coverage of the exact clip interval is required; late start or short end is `coverage_missing`, never clamped.
  Acceptance per moment: exactly one video stream and no other streams, dimensions equal to the planned rendition, square pixels, no rotation, SDR/8-bit (`yuv420p`/`yuvj420p` only), full decode, and frame count/max-gap checks <= one frame interval INSIDE THE DELIVERED CLIP WINDOW; fragment-boundary holes in the wider acquisition margins are tolerated when outside that window. Final clip validation is in [720, max_height].
  Outputs: out/<theme>/clips/<video_id>_e<idx>_<start_ms>-<end_ms>.mp4 + schema-2 manifest.json (counts, max_height, per-clip identity/timestamps/mapping K/format id/dims/acquisition span/duration/sha256/acq_sha256, per-failure reason codes), and schema-2 run_dir/export.json written last. Re-runs reuse acquisitions and clips only when the full binding matches (identity, interval, span, K, format, acquisition sha256, recipe, ffmpeg version).
  Exit codes: 0 all selected moments exported; 1 stale inputs or any per-moment failure (recorded, never downgraded); 2 invalid invocation, missing authorization, corrupt/foreign theme, unsafe paths, or insufficient disk. `export_complete` and `request_fulfilled` are reported separately (8 of 20 is complete but not fulfilled). See docs/EXPORT.md.

chain_parts123.py
  Live: run → rank → analyze → verify for the European scenery example (5 hits).
  Does not apply vision. PATH: invoked as `.venv/bin/python scripts/chain_parts123.py`.

Exit codes: 0 complete/valid, 1 operationally partial/failed/stale, 2 bad arguments or invalid config.
