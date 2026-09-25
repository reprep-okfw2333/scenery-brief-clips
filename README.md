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
unchanged in behavior (plus one optional, off-by-default pre-rank filter, the
Jev gate, below):

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
  jev-gate CLI (experimental)         OPTIONAL, off by default (jev_gate: true
    src/scenery_brief_clips/jev_gate.py  to enable). Runs after discovery and
                                      before rank/tile review; asks
                                      typesafe/jev-1.13 (OpenRouter decisions
                                      endpoint) for P(keep) from metadata and
                                      rejects only when P(keep) <= 0.40. Never
                                      auto-keeps. Key only from the
                                      OPENROUTER_API_KEY env var; no key, errors,
                                      timeouts, or the per-run cost cap fall back
                                      to the rule gate. ~$0.07 per 1,000
                                      candidates. See docs/JEV_GATE.md.

Honest limits (measured, not assumed):
  - The planner only shapes SEARCH QUERIES. It does not and cannot make the
    visual stages enforce the brief. Tile/strip labels, scene detection, and
    export acceptance are exactly as permissive as in scenery-clips.
  - Every search hit is marked visual_status=unverified,
    acceptance_level=metadata_only. "Candidate" never means "verified clip".
  - Explicit scene exclusions in a brief (e.g. "no people") are recorded
    requirements, not enforced gates. An operator must still review output.
  - The Jev gate reads titles/descriptions only: it cannot detect watermarks,
    and its 0.40 cutoff was tuned on train footage (one reviewer's labels).
    Offline on 96 labeled train candidates: accuracy 0.74 vs 0.49 for the rule
    gate alone, ~30% fewer tile reviews, only ~2–3% less analyze download.
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

  # Analyze (shared with those downstream stages) runs spans in parallel by default (SCENERY_ANALYZE_WORKERS=4; cold bench analyze ~73s / pipeline ~139s on 2026-09-24; see docs/CLI.md). Downstream stages are the inherited scenery-clips commands (rank, analyze, ...):
  .venv/bin/scenery-brief-clips rank --run-dir data/runs/<id> --max-videos 5 --max-tiles 8

  # Optional Jev metadata gate (off by default). Needs jev_gate: true in the
  # config and OPENROUTER_API_KEY exported in the environment (never in a file).
  # Run it after run/run-brief and BEFORE rank:
  .venv/bin/scenery-brief-clips jev-gate --run-dir data/runs/<id> --config config.yaml

The planner model switch is planner.yaml (codex-login / gpt-6-sol by default,
mirroring vision.yaml's rules: no secrets in the file, explicit confirmation
culture). --plan skips the model call entirely and uses a pre-validated plan.

## Tests

  .venv/bin/python -m pytest tests/ -q
Last recorded full-suite run: 358 passed on 2026-09-25 (309 inherited + 32
brief/planner/run-brief + 2 analyze env-default + 15 Jev gate tests; all
offline, the Jev endpoint is stubbed). The run-brief path was additionally smoke-tested
live end-to-end with a frozen plan (metadata-only; 6 candidates; no download).

## Requires

Linux; Python 3.14; yt-dlp, ffmpeg, ffprobe, Node.js. Project tmp/ only (never
host /tmp for media). data/, tmp/, out/ are gitignored.

## Rights and boundaries (inherited, plus this project's own)

YouTube terms restrict downloading; discovery is metadata-only and run-brief
can never fetch video, call vision, or export. The optional jev-gate sends only
metadata text (title, channel, tags, trimmed description, counts) to OpenRouter;
it never downloads video. The inherited export path still
requires explicit --allow-export. Every yt-dlp call ignores host config.

## Docs

  docs/STATUS.md          current snapshot — read first
  docs/ARCHITECTURE.md    full pipeline including the new brief/planner stages
  docs/SEARCH_BRIEF.md    the Part 7 design this project implements
  docs/ISSUES.md          open problems — read with STATUS
  docs/CLI.md             commands
  docs/JEV_GATE.md        optional Jev metadata gate (off by default): setup,
                          config keys, fallbacks, outputs, cost, evaluation, limits
  AGENTS.md               rules for agents in this repo