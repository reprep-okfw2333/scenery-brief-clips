# scenery-clips (Hermes)

Work only inside this project root (the scenery-clips checkout). Do not scatter caches, notes, or clones.

Read docs/STATUS.md first, then docs/ISSUES.md, docs/ARCHITECTURE.md, and docs/ROADMAP.md.

- CLI: `.venv/bin/scenery-clips ...` (uv is often not on PATH).
- Temps: project `tmp/` only. Never host /tmp for media.
- Part 2: rank saves tiles. Picture labels come from the model named in vision.yaml (`vision-show`, then `label-tiles --confirm-vision`). Show that model and get a yes before a run. Do not put a key in vision.yaml. Then apply-scores, analyze, and verify. Dark tiles are reject and are not sent.
- Part 3: selected-window ≤720p video-only, time-capped; continuity gate trims/rejects mid-excerpt dissolves. Span/policy cache keys + validated SHA-256 markers; never YtDlp.download(). IDs may start with `-`; use watch URLs.
- Every yt-dlp call uses `--ignore-config`. Search/metadata use --skip-download. Analysis uses --download-sections and only `bv`/`wv` selectors with `[height<=720]`, preferring AVC/H.264.
- Cookies off unless the user opts in.
- Do not silently lower resolution, aspect, or N.
- Optional flat config: project config.yaml or `--config`; explicit flags win. Config may tighten geometry but never loosen the prompt. Downloads stay forbidden in the default pipeline; the Part 5 export path is authorized explicitly (`allow_export: true` or `--allow-export`) and only ever acquires shortlisted sections at the resolved cap rendition (largest video-only rendition at or under `export_max_height`, default 720p, never below the 720p floor).
- Metadata cache is reusable; prompt-specific files stay in the run dir.
- `verify` is run-scoped and fail-closed: input/output hashes, exact plan/range/copy reconciliation, completion markers, probe, duration, and strict decode.
- Tests: `.venv/bin/python -m pytest tests/ -q`. Prefer fixtures over live YouTube.
- Expected environment: Linux + Python 3.14. Run/export locks use fcntl (not available on native Windows).
- Shortlist: `continuity_suspect` + keep without `continuity_ok:` / `continuity_ok: true` → exclude `continuity_suspect_uncleared`.
- Export preserves approved shortlist intervals (no continuity re-trim).
- After a behavior change, update docs/STATUS.md, docs/ISSUES.md if the open-problem list changed, and the matching doc.
- Parts 1–6 are implemented. Do not start a new part until the user explicitly says to, and not before done criteria are written. The Part 6 skill is `.hermes/skills/scenery-clips/SKILL.md`.
