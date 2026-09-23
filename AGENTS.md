# scenery-brief-clips (Hermes)

This is scenery-brief-clips: an independent fork of scenery-clips with a
frozen-brief + one-call query-planner discovery stage (Part 7A/7B/7C) added on
top of the inherited Parts 1–6. Work only inside this project root. Do not
scatter caches, notes, or clones. Do not write to /root/projects/scenery-clips;
it is the read-only original.

Read docs/STATUS.md first, then docs/ISSUES.md, docs/ARCHITECTURE.md, and
docs/SEARCH_BRIEF.md (the Part 7 contract this fork implements).

- CLI: `.venv/bin/scenery-brief-clips ...` (uv is often not on PATH).
- Two discovery paths: legacy `run --dry-run --prompt` (unchanged behavior)
  and new `run-brief --brief <path> --dry-run [--plan <path>]`. The planner
  model is named in planner.yaml, mirroring vision.yaml's no-secrets rules.
  `--plan` skips the model call. Do not put a key in planner.yaml or vision.yaml.
- The planner makes ONE bounded call proposing search phrases; the app
  validates the plan deterministically afterward. There is no fallback search
  that broadens the brief; validation failures and empty plans stop fail-closed.
- Provenance rule: search hits are leads, never verified clips. Every run-brief
  result is visual_status=unverified / acceptance_level=metadata_only. Do not
  present candidates as on-brief clips; downstream visual stages are unchanged
  and do not enforce brief scene fields or exclusions. Operator review before
  delivering files remains mandatory.
- Temps: project `tmp/` only. Never host /tmp for media.
- Part 2: rank saves tiles. Picture labels come from the model named in vision.yaml (`vision-show`, then `label-tiles --confirm-vision`). Show that model and get a yes before a run. Dark tiles are reject and are not sent.
- Part 3: selected-window ≤720p video-only, time-capped; continuity gate trims/rejects mid-excerpt dissolves. Span/policy cache keys + validated SHA-256 markers; never YtDlp.download(). IDs may start with `-`; use watch URLs.
- Every yt-dlp call uses `--ignore-config`. Search/metadata use --skip-download. Analysis uses --download-sections and only `bv`/`wv` selectors with `[height<=720]`, preferring AVC/H.264.
- Cookies off unless the user opts in.
- Do not silently lower resolution, aspect, or N.
- Optional flat config: project config.yaml or `--config`; explicit flags win. Config may tighten geometry but never loosen the prompt. Downloads stay forbidden in the default pipeline; the Part 5 export path is authorized explicitly (`allow_export: true` or `--allow-export`) and only ever acquires shortlisted sections at the resolved cap rendition (largest video-only rendition at or under `export_max_height`, default 720p, never below the 720p floor).
- run-brief can NEVER fetch video, call vision, or export. Its limit flags only tighten the brief's own search_limits (final = min).
- Metadata cache is reusable; prompt-specific files stay in the run dir.
- `verify` is run-scoped and fail-closed: input/output hashes, exact plan/range/copy reconciliation, completion markers, probe, duration, and strict decode. The verifier checks file integrity, not scene fidelity.
- Tests: `.venv/bin/python -m pytest tests/ -q` (341 passing as of 2026-09-23). Prefer fixtures over live YouTube.
- Expected environment: Linux + Python 3.14. Run/export locks use fcntl (not available on native Windows).
- Shortlist: `continuity_suspect` + keep without `continuity_ok:` / `continuity_ok: true` → exclude `continuity_suspect_uncleared`.
- Export preserves approved shortlist intervals (no continuity re-trim).
- After a behavior change, update docs/STATUS.md, docs/ISSUES.md if the open-problem list changed, and the matching doc.
- Inherited Parts 1–6 plus this fork's Part 7A/7B/7C are implemented. Do not
  start a new part until the user explicitly says to, and not before done
  criteria are written.