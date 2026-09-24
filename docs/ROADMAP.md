# Roadmap

Status is the code in this repo plus docs/STATUS.md, not chat history.
Done means: tests in tests/, a CLI command, and docs updated.

## Built

Part 1 — done
  Prompt compile, multi-query YouTube search, metadata eligibility (default ≥720p / 1280×720 when no resolution is named, 16:9, not live).
  Earlier search hits survive a later query error; metadata cache entries remain usable after the network-fetch budget is exhausted.
  CLI: doctor, explain-prompt, run --dry-run
  Flat project config.yaml / --config defaults are supported; flags override config and prompt geometry cannot be loosened.
  Explicit 1920p in a prompt means 1920×1080; explicit 720p and 1280×720 mean 1280×720.

Part 2 — done (tiles + external vision scores)
  Sample storyboard tiles, save JPEGs under data/cache/tiles/.
  Without scores, every non-dark video is uncertain.
  apply-scores reads vision_scores.json and sets promising / uncertain / low + windows.
  CLI: rank, vision-show, label-tiles, apply-scores
  The model is named in vision.yaml. Tile labels use at most two concurrent model calls; results retain ranked order. See docs/ISSUES.md for measured latency and remaining limits.

Part 3 — done and stabilized
  ≤720p video-only copies, preferring AVC/H.264, of planned ranges (windowed / full / spread), cap seconds.
  Scored windows are authoritative; over-budget windows are capped internally rather than replaced by unscored spread sampling.
  Span/policy cache keys + SHA-256 completion markers; locked staged downloads; legacy index-only cache is ignored.
  Planned spans run concurrently (default 4 workers; SCENERY_ANALYZE_WORKERS). Shared by legacy run and run-brief.
  PySceneDetect min_scene_len 0.5s, default frame_skip=1 (SCENERY_DETECT_FRAME_SKIP). Long shots → target-length interior excerpt.
  Continuity gate (~2 fps dHash + mean-RGB; default decode width 160 via SCENERY_CONTINUITY_DECODE_WIDTH) trims/rejects soft dissolves PySceneDetect misses.
  Per-range outcomes preserve all attempt errors; partial/failed work exits nonzero.
  excerpts.json + analysis_manifest.json bind outputs to ranked/constraint/settings and publish fail-closed.
  CLI: analyze (default --max-videos 1)
  Full-quality download still forbidden.

Verify — done
  CLI: verify
  Recomputes the plan and checks exact row/range/copy completeness, input/output hashes, cache markers, referenced paths only, geometry, duration, no audio, ≤720p, and strict decode.
  Stale ranked/constraint/excerpts generations fail. Unrelated shared-cache files are ignored.

Chain — done, limited
  scripts/chain_parts123.py : run → rank → analyze → verify (5 European-scenery candidates).
  Does not apply vision scores.

## Waiting (Part 5 complete)

Part 4 — done (2026-09-21; design + rules in docs/SHORTLIST.md)
  Temporal review: shortlist-review samples frames across each candidate moment
  (default 6; always includes near-start/near-end; source times; extraction maps
  via the analysis_span offset), saves frames + a strip per moment under review/,
  and writes review.json bound to the excerpts generation. Vision stays external
  (same pattern as Part 2); reject strips that show a mid-moment cut/dissolve.
  Rules: conflicting geography never selected; geo-uncertain selected only with
  flags ["geo_uncertain"]; non-keep and unscored excluded with reasons.
  Dedup: 64-bit dHash per frame, min Hamming <= 10 collapses near-identical
  moments ("duplicate of <video>:<index>"). Diversity: round-robin over
  scene_type groups, least-used sources first.
  Output: deterministic shortlist.json (selected, excluded+reasons, counts,
  shortfall explanation). Never loosens the brief or pads to hit N.
  CLI: shortlist-review, shortlist-apply; verify checks shortlist.json when
  present (stale/tampered fails closed).
  Done criteria: all 10 met — 151 tests total (31 new), live pass
  20260918T210550Z (8 selected / 4 excluded / shortfall 12, explained), fixture
  demo data/runs/dupdemo-demo (duplicate collapse, verify ok), byte-identical
  re-apply, docs updated.
  Live-pass fix recorded: review extraction originally sent source timestamps
  to ffmpeg on the copy (wrong region / past EOF) — fixed via analysis_span
  offset mapping + regression test.

Part 5 — done (2026-09-22; design + rules + done criteria in docs/EXPORT.md; live pass under the amended 720p-cap contract)
  AMENDED 2026-09-22: the default source floor is ≥720p (1280×720) when
  the prompt names no resolution; explicit prompt resolutions still gate to
  their requested dimensions. Export delivers the largest native 16:9,
  video-only rendition at or under `export_max_height` (default 720p; valid
  integers 720..2160), never below the 720p floor and never scaled.
  Download only shortlisted moments' acquisition ranges: yt-dlp
  --download-sections, stream copy with copyts (absolute timestamps; no
  acquisition encode), pinned format id. Acquire slightly wide (margin
  2s/side, clamped), export the interior with ONE frame-accurate encode
  (libx264 crf 17 preset medium, video-only).
  Coverage-based acquisition validation + full probe/decode; HDR/10-bit/
  non-square-pixel fail closed; no downgrade, no padding, no substitutes.
  Folder out/<theme>/clips/ + schema-2 manifest.json (with max_height);
  schema-2 run_dir/export.json pointer; verify proves the cap band and
  provenance (partition, hashes, probes, acquisitions_checked;
  --require-export). CLI: export. Done when: docs/EXPORT.md done criteria
  all met (tests, CLI, live pass, demos, docs), still no full-film downloads.

Part 6 — done (2026-09-22; skill + 3-clip live pass)
  Skill: `.hermes/skills/scenery-clips/SKILL.md`. Loads for a session whose
  cwd is this checkout after `hermes skills trust` (verified by a fresh
  process: trusted root, skill file not quarantined).
  Live pass: data/runs/20260922T160610Z. Prompt asked for 3 clips at 720p.
  verify --require-export ok, exported 3, failed 0.
  Clips: out/part6-european-scenery-3/clips/ plus schema-2 manifest.json.
  The default theme folder was already owned by 20260918T210550Z, so this
  export used --theme part6-european-scenery-3 and did not touch that folder.

## Proposed next part — not started

Part 7: frozen request brief and model-assisted YouTube query planning. The
contract, fixed worker prompt, subparts 7A–7C, and testable done criteria are
in docs/SEARCH_BRIEF.md. Vision and the later pipeline are out of scope for
this part. This is a design, not an implemented command or a visual gate;
confirm the contract before coding.

## Later-part boundaries

  Geo evidence fields are Part 4.
  Cover thumbnails are intentionally excluded from timeline scoring; sources without storyboards remain unknown.
