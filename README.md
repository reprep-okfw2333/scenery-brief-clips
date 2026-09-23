# scenery-clips

Find YouTube scenery candidates cheaply, then export cap-limited real clips. Everything for this project lives in this directory.

  <project-root>   # this repository; discover with the checkout path

Start here: docs/STATUS.md (what is done, latest run, what is not built).
Do not put run output under /tmp or ~/.hermes. data/ and tmp/ are gitignored.

Quick start

  cd <project-root>
  .venv/bin/scenery-clips doctor
  .venv/bin/python -m pytest tests/ -q

  .venv/bin/scenery-clips vision-show   # tell the user the model and get agreement before any live run
  .venv/bin/scenery-clips explain-prompt "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips"
  # A prompt with no resolution defaults to ≥720p / 1280×720; 720p is explicit too:
  .venv/bin/scenery-clips explain-prompt "Beautiful natural european scenery, 720p, 16:9, 20 individual clips"
  .venv/bin/scenery-clips run --dry-run --prompt "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips" --max-results 5 --sleep 2
  .venv/bin/scenery-clips rank --run-dir data/runs/<id> --max-videos 5 --max-tiles 8
  # Only after the user agrees to the exact model shown by vision-show:
  .venv/bin/scenery-clips label-tiles --run-dir data/runs/<id> --confirm-vision
  .venv/bin/scenery-clips apply-scores --run-dir data/runs/<id> --scores data/runs/<id>/vision_scores.json
  .venv/bin/scenery-clips analyze --run-dir data/runs/<id> --max-videos 5 --max-analysis-s 120
  .venv/bin/scenery-clips verify --run-dir data/runs/<id>   # require ready_for_shortlist=true
  .venv/bin/scenery-clips shortlist-review --run-dir data/runs/<id>   # default --frames 6
  .venv/bin/scenery-clips label-strips --run-dir data/runs/<id> --confirm-vision
  .venv/bin/scenery-clips shortlist-apply --run-dir data/runs/<id> --scores data/runs/<id>/shortlist_scores.json
  # Only if the user authorized section downloads and wants files on disk:
  .venv/bin/scenery-clips export --run-dir data/runs/<id> --allow-export
  .venv/bin/scenery-clips verify --run-dir data/runs/<id> --require-export

  .venv/bin/python scripts/chain_parts123.py   # live parts 1–3 + verify; does not apply vision

Requires: Linux, yt-dlp, ffmpeg, ffprobe, Node.js (for yt-dlp JS runtimes), Python 3.14. Native Windows is unsupported for run locks (fcntl); use WSL/Linux. Prefer `.venv/bin/...` (`uv` is often not on PATH). TMPDIR is project tmp/.
Optional defaults: copy config.example.yaml to config.yaml, or pass `--config config.example.yaml` to run/rank/analyze/export. Explicit flags win; config cannot enable downloading or loosen prompt geometry. A prompt with no resolution defaults to a 1280×720 source floor; explicit 720p and 1280×720 map to 1280×720, while explicit 1920p maps to 1920×1080. Export uses the largest native 16:9 video-only rendition at or under `export_max_height` (default 720p; valid 720..2160), never scaling it.

Docs

  docs/STATUS.md          current snapshot — read first
  docs/ISSUES.md          open problems — read with STATUS
  docs/ARCHITECTURE.md    full pipeline (A–F)
  docs/ROADMAP.md         parts 1–6 and done criteria
  docs/VISION.md          Part 2 tile scoring
  docs/SHORTLIST.md       Part 4 moment scoring, dedup, diversity, shortfall
  docs/DATA.md            on-disk layout
  docs/CLI.md             commands
  AGENTS.md               rules for Hermes in this repo

Tests: the last recorded complete-suite run passed 309 tests during the
2026-09-22 tree-cutting stabilization pass (docs/STATUS.md). This README
update did not rerun the suite. For the latest delivered run and its corrected
counts, see docs/RUN-20260922T205123Z-ocean-waves.md.

Rights: YouTube terms restrict downloading. Parts 1–2 are metadata and storyboard JPEGs. Part 3 fetches only selected, validated, capped ≤720p video-only ranges. Part 5 is the explicitly authorized path for shortlisted section acquisitions and final clips at the configured cap (default 720p); it never downloads a full film. Every yt-dlp call ignores host configuration; cookies remain off unless an explicit future policy opts in.

Parts 1–6 are done. Picture labels use the model named in vision.yaml (see docs/VISION.md). Hermes must show that model and get a yes before a run. Part 5 publishes one-encode, video-only, native-16:9 clips using the largest rendition at or below the cap. See docs/EXPORT.md. Part 6 is the Hermes skill at `.hermes/skills/scenery-clips/SKILL.md` plus a 3-clip delivery under out/part6-european-scenery-3/. Shortlist `continuity_suspect` needs an explicit `continuity_ok:` clearance to keep; shortfalls report upstream continuity rejects.
