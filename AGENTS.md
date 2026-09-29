# scenery-brief-clips (Hermes)

This is scenery-brief-clips: an independent fork of scenery-clips with a
frozen-brief + one-call query-planner discovery stage (Part 7A/7B/7C) added on
top of the inherited Parts 1–6. Work only inside this project root. Do not
scatter caches, notes, or clones. Do not write to /root/projects/scenery-clips;
it is the read-only original.

Read docs/STATUS.md first, then docs/ISSUES.md, docs/ARCHITECTURE.md, and
docs/SEARCH_BRIEF.md (the Part 7 contract this fork implements). The optional
Jev ranking / note check / gate is documented in docs/JEV.md.

- CLI: `.venv/bin/scenery-brief-clips ...` (uv is often not on PATH).
- Two discovery paths: legacy `run --dry-run --prompt` (unchanged behavior)
  and new `run-brief --brief <path> --dry-run [--plan <path>]`. The one-command
  runner `run-pipeline --brief <path>` takes `--plan <path>` or `--live-planner`.
  The planner model is named in planner.yaml, mirroring vision.yaml's
  no-secrets rules. `--plan` skips the model call. Do not put a key in
  planner.yaml or vision.yaml.
- The planner makes one bounded call proposing search phrases (prompt v2
  states the validator's exact-subject rule); the app validates the plan
  deterministically. A reply that is not JSON or fails validation gets ONE
  corrective retry with the rejection text; network/auth errors are not
  retried. There is no fallback search that broadens the brief; a second
  rejection or an empty plan stops fail-closed.
- Provenance rule: search hits are leads, never verified clips. Every run-brief
  result is visual_status=unverified / acceptance_level=metadata_only. Do not
  present candidates as on-brief clips; downstream visual stages are unchanged
  and do not enforce brief scene fields or exclusions. Operator review before
  delivering files remains mandatory.
- Temps: project `tmp/` only. Never host /tmp for media.
- Part 2: rank saves tiles. Picture labels come from the model named in vision.yaml (`vision-show`, then `label-tiles --confirm-vision`). Show that model and get a yes before a run. Dark tiles are reject and are not sent.
- Part 3: selected-window ≤720p video-only, time-capped; spans analyze concurrently (default 2 workers; 4 swapped this 1.6 GB host). Each span download has a hard timeout of 90 s + 10 s per span second (max 300 s). run-pipeline stops with status `deadline` once `run_deadline_s` (config, default 10800) is spent; rerun the same command to resume. When YouTube refuses the host ("Sign in to confirm you're not a bot") it stops with status `blocked`: wait (usually hours) and rerun the same command; do not retry in a loop. Analysis windows are at least `min_window_s` = max(duration_min + 2 x pad, target) long (manifest setting; absent = 0 for older runs). A span whose every download holds no streams (the source's video track ends early) is recorded `unavailable` (`no_video_in_span`) and does not fail the run; verify accepts only that exact form, as a warning. Continuity gate trims/rejects mid-excerpt cuts and dissolves: default detector `blend` (motion-tolerant, 6 fps; continuity_blend.py), `continuity_detector: legacy` restores the old dHash/mean-RGB thresholds (decode width 160). Scene detect defaults to frame_skip=1. Legacy serial/full-frame: SCENERY_ANALYZE_WORKERS=1 SCENERY_DETECT_FRAME_SKIP=0 SCENERY_CONTINUITY_DECODE_WIDTH=0. Span/policy cache keys + validated SHA-256 markers; never YtDlp.download(). IDs may start with `-`; use watch URLs.
- Every yt-dlp call uses `--ignore-config`. Search/metadata use --skip-download. Analysis uses --download-sections as stream copy with `-copyts` (no `--force-keyframes-at-cuts`, no re-encode), one yt-dlp call per video for all its spans. It pins the export rendition when that is ≤720p (so export can adopt the same file), else only `bv`/`wv` selectors with `[height<=720]`, preferring AVC/H.264. Local time 0 of an analysis copy is its first packet PTS (marker `first_pts_ms`, range `mapping_k_s`), not the span start.
- Cookies off unless the user opts in. **The YouTube fallback below is NOT YET FUNCTIONAL (never run live) and OFF; do not enable or rely on it without the owner.** The opt-in is config `youtube_cookies_file` (a Netscape cookies.txt OUTSIDE the project, ideally a throwaway account) and/or `youtube_proxy`; they are used only after YouTube refuses the host (bot check), and then for the rest of that process. Each yt-dlp call gets a private 0600 copy of the cookies (deleted after); never print, copy into the repo, or log the cookies file or proxy URL. Without a fallback, the first refusal trips a circuit breaker: no further YouTube requests in that process, status `blocked`.
- Do not silently lower resolution, aspect, or N.
- Optional flat config: project config.yaml or `--config`; explicit flags win. Config may tighten geometry but never loosen the prompt. Downloads stay forbidden in the default pipeline; the Part 5 export path is authorized explicitly (`allow_export: true` or `--allow-export`) and only ever acquires shortlisted sections at the resolved cap rendition (largest video-only rendition at or under `export_max_height`, default 720p, never below the 720p floor).
- Optional Jev (docs/JEV.md), all off by default: `jev_rank: true` makes run-pipeline's discovery order search
  hits by a `typesafe/jev-1.13` score (mean of P(usable), P(subject), P(conditions); questions `jev_source_q_v2`)
  and reject candidates at score <= `jev_reject_below` (0.35) after their metadata; `jev_note_check: true`
  excludes vision `keep` moments whose own note reports a failure (P(violation) >= `jev_note_reject_at`, 0.70;
  `note_violation` lives in shortlist_scores.json so verify reproduces it); `jev_gate: true` enables the manual
  `jev-gate --run-dir RUN [--brief B]` (after run/run-brief, BEFORE rank). Jev only rejects/reorders, never
  auto-keeps; tile review, vision and shortlist review stay mandatory. Text only: it cannot see watermarks.
  The key comes only from the `OPENROUTER_API_KEY` environment variable: never write a key into config, yaml,
  docs, logs, commits, or PR text. No key, errors, timeouts, or `jev_max_usd_per_run` fall back (recorded).
  Cache data/cache/jev/. Jev keys enter runner bindings only when switched on.
- run-brief can NEVER fetch video, call vision, or export. Its limit flags only tighten the brief's own search_limits (final = min).
- Metadata cache is reusable; prompt-specific files stay in the run dir.
- `verify` is run-scoped and fail-closed: input/output hashes, exact plan/range/copy reconciliation, completion markers, probe, duration, and strict decode. The verifier checks file integrity, not scene fidelity. Inside run-pipeline, verify_export skips re-decoding analysis media that verify_review already decoded when the bytes' SHA-256 still matches (`decoded_media` in verify_review.json); the standalone `verify` decodes everything.
- run-pipeline's discovery stops fetching metadata once `max_rank_videos` candidates are kept (rank uses only those, in search order); discovery.json records `max_candidates`.
- Tests: `.venv/bin/python -m pytest tests/ -q` (958 passed, 1 xfailed as of 2026-09-29; about 7 min on this host). Never two pytest processes at once (shared tmp/pytest). Prefer fixtures over live YouTube.
- Vision labeling (tiles and strips) runs `SCENERY_VISION_WORKERS` concurrent model calls (default 4, 1..8); results keep review order.
- Expected environment: Linux + Python 3.14. Run/export locks use fcntl (not available on native Windows).
- Shortlist: `continuity_suspect` + keep without `continuity_ok:` / `continuity_ok: true` → exclude `continuity_suspect_uncleared`.
- Export preserves approved shortlist intervals (no continuity re-trim).
- After a behavior change, update docs/STATUS.md, docs/ISSUES.md if the open-problem list changed, and the matching doc.
- Inherited Parts 1–6 plus this fork's Part 7A/7B/7C are implemented. Do not
  start a new part until the user explicitly says to, and not before done
  criteria are written.