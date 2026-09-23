# scenery-brief-clips

An independent fork of scenery-clips that adds a **frozen request brief and a
one-call model query planner** ahead of the existing search funnel. It exists
to run the "Part 7" design (docs/SEARCH_BRIEF.md) as real, tested code without
touching the original scenery-clips checkout, which stays on Parts 1–6.

Everything for this project lives in this directory. It has its own git
history and its own venv. The original is at /root/projects/scenery-clips and
is read-only as far as this project is concerned.

## What is actually different from scenery-clips

One new discovery path; everything downstream (rank → vision labels → analyze
→ shortlist → export → verify) is the inherited scenery-clips machinery,
unchanged in behavior:

  OLD (scenery-clips): prompt string → build_queries() → search
  NEW (this project):  brief JSON (validated, hash-frozen) →
                       one bounded model call (or a frozen plan file) →
                       deterministic query-plan validation → same search

New pieces (implemented, tested):
  src/scenery_brief_clips/brief.py    strict search_brief_v1 contract, planner
                                      instruction, query-plan validation
  src/scenery_brief_clips/planner.py  one bounded model call returning a
                                      validated search_queries_v1 plan; records
                                      provenance (model, version, seconds, hash)
  planner.yaml                        planner model switch (no secrets; same
                                      rules as vision.yaml)
  run_dry(queries=...)                optional query injection; legacy prompt
                                      path is byte-identical when absent
  run-brief CLI                       brief → plan → metadata-only discovery;
                                      writes discovery.json sidecar

Honest limits (measured, not assumed):
  - The planner only shapes SEARCH QUERIES. It does not and cannot make the
    visual stages enforce the brief. Tile/strip labels, scene detection, and
    export acceptance are exactly as permissive as in scenery-clips.
  - Every search hit is marked visual_status=unverified,
    acceptance_level=metadata_only. "Candidate" never means "verified clip".
  - Explicit scene exclusions in a brief (e.g. "no people") are recorded
    requirements, not enforced gates. An operator must still review output.
  - Benchmark evidence (2026-09-23, /root/experiments/scenery-brief-lab):
    on an easy request the planner performed on par with the legacy query
    builder (3/3 clips delivered both ways, ~19 min common stages each); on a
    hard request (alpacas in a field) both arms produced zero strictly usable
    clips. One pass each; not a general performance claim.

## Quick start

  cd /root/projects/scenery-brief-clips
  .venv/bin/scenery-brief-clips doctor
  .venv/bin/python -m pytest tests/ -q

  .venv/bin/scenery-brief-clips explain-prompt "3 clips of ocean waves, 720p, 16:9"

  # New structured path (metadata-only discovery; no video download):
  .venv/bin/scenery-brief-clips run-brief --brief examples/alpaca-brief.json --dry-run
  #   ...or with a frozen plan file instead of a live planner call:
  .venv/bin/scenery-brief-clips run-brief --brief brief.json --dry-run --plan plan.json

  # Legacy prompt path (unchanged):
  .venv/bin/scenery-brief-clips run --dry-run --prompt "Beautiful natural european scenery, 720p, 16:9, 20 individual clips"

  # Downstream stages are the inherited scenery-clips commands (rank, analyze, ...):
  .venv/bin/scenery-brief-clips rank --run-dir data/runs/<id> --max-videos 5 --max-tiles 8

The planner model switch is planner.yaml (codex-login / gpt-6-sol by default,
mirroring vision.yaml's rules: no secrets in the file, explicit confirmation
culture). --plan skips the model call entirely and uses a pre-validated plan.

## Tests

  .venv/bin/python -m pytest tests/ -q
Last recorded full-suite run: 341 passed (309 inherited + 32 new brief/planner/
run-brief tests, all offline). The run-brief path was additionally smoke-tested
live end-to-end with a frozen plan (metadata-only; 6 candidates; no download).

## Requires

Linux; Python 3.14; yt-dlp, ffmpeg, ffprobe, Node.js. Project tmp/ only (never
host /tmp for media). data/, tmp/, out/ are gitignored.

## Rights and boundaries (inherited, plus this project's own)

YouTube terms restrict downloading; discovery is metadata-only and run-brief
can never fetch video, call vision, or export. The inherited export path still
requires explicit --allow-export. Every yt-dlp call ignores host config.

## Docs

  docs/STATUS.md          current snapshot — read first
  docs/ARCHITECTURE.md    full pipeline including the new brief/planner stages
  docs/SEARCH_BRIEF.md    the Part 7 design this project implements
  docs/ISSUES.md          open problems — read with STATUS
  docs/CLI.md             commands
  AGENTS.md               rules for agents in this repo