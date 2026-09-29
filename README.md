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
  Jev decisions (optional)            OFF by default. typesafe/jev-1.13 via the
    src/scenery_brief_clips/jev.py      OpenRouter decisions endpoint (text only).
                                      run-pipeline: jev_rank orders search hits
                                      and rejects weak candidates in discovery;
                                      jev_note_check excludes vision keep moments
                                      whose own note reports a failure. Manual
                                      CLI: jev-gate. Never auto-keeps; key only
                                      from OPENROUTER_API_KEY; any failure falls
                                      back. See docs/JEV.md.

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

  cd /root/github-checkouts/reprep-okfw2333/scenery-brief-clips
  .venv/bin/scenery-brief-clips doctor
  .venv/bin/python -m pytest tests/ -q

  .venv/bin/scenery-brief-clips explain-prompt "3 clips of ocean waves, 720p, 16:9"

  # New structured path (metadata-only discovery; no video download):
  # (example briefs: benchmark/{improvement,military,heldout-horses,step4-reddeer}/brief.json)
  .venv/bin/scenery-brief-clips run-brief --brief benchmark/step4-reddeer/brief.json --dry-run
  #   ...or with a frozen plan file instead of a live planner call:
  .venv/bin/scenery-brief-clips run-brief --brief brief.json --dry-run --plan plan.json

  # One command, brief to verified clips (live planner + live vision + export):
  .venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --live-planner \
      --vision-agree --live-vision --allow-export --theme NAME

  # Legacy prompt path (unchanged):
  .venv/bin/scenery-brief-clips run --dry-run --prompt "Beautiful natural european scenery, 720p, 16:9, 20 individual clips"

  # Analyze (shared with those downstream stages) runs spans in parallel by default (SCENERY_ANALYZE_WORKERS=2 since 2026-09-28; the 4-worker cold bench measured analyze ~73s / pipeline ~139s on 2026-09-24; see docs/CLI.md). Downstream stages are the inherited scenery-clips commands (rank, analyze, ...):
  .venv/bin/scenery-brief-clips rank --run-dir data/runs/<id> --max-videos 5 --max-tiles 8

  # Optional Jev metadata gate (off by default). Needs jev_gate: true in the
  # config and OPENROUTER_API_KEY exported in the environment (never in a file).
  # Run it after run/run-brief and BEFORE rank:
  .venv/bin/scenery-brief-clips jev-gate --run-dir data/runs/<id> --config config.yaml

The planner model switch is planner.yaml (openai-api / z-ai/glm-5.3-flash on
OpenRouter since 2026-09-27, key from OPENROUTER_API_KEY;
mirroring vision.yaml's rules: no secrets in the file, explicit confirmation
culture). --plan skips the model call entirely and uses a pre-validated plan.

## Tests

  .venv/bin/python -m pytest tests/ -q
Last recorded full-suite run: 607 passed, 1 xfailed on 2026-09-29 (all
offline; model endpoints are stubbed). History: 358 passed on 2026-09-25, when
the run-brief path was also smoke-tested live with a frozen plan
(metadata-only; 6 candidates; no download).

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
  docs/JEV.md             optional Jev ranking, note check and manual gate (off by
                          default): questions, config keys, fallbacks, cost, evidence, limits
  AGENTS.md               rules for agents in this repo