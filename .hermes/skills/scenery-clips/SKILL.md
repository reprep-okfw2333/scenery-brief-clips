---
name: scenery-clips
description: Use when making YouTube scenery clips from a prompt.
version: 0.1.0
author: Luigi Manzi (reprep-okfw2333), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    editorial_name: Scenery Clips
    editorial_description: Turn a scenery sentence into a few checked YouTube clips without quietly lowering the brief.
    tags: [YouTube, Scenery, Clips, Pipeline]
    requires_tools: [terminal, process_manage, clarify, read_file, write_file]
    requires_toolsets: []
    requires_plugins: []
---

# Scenery Clips

Turn one scenery sentence into short on-disk clips. Search and stills are cheap. Video is fetched only for chosen windows, and only at the export cap after an explicit allow. If fewer than N pass, return what passed and why. Never pad, and never quietly lower resolution, aspect, or N.

Picture labels come from whatever model `vision.yaml` names. That file is the switch. It is not a secret file. Changing the model means editing `vision.yaml`, not the Python.

## When to Use

- The user wants scenery clips, a scenery shortlist, or a rerun of this funnel.
- A run dir already exists and the next stage (rank, analyze, shortlist, export, verify) is the ask.

Don't use for: general YouTube download, full-film rips, or any brief this repo's prompt compiler cannot express. Do not start a new pipeline part that docs/ROADMAP.md still marks unbuilt.

## Prerequisites

- Linux. Run locks use fcntl; native Windows is unsupported.
- Project checkout with `.venv/bin/scenery-clips`, yt-dlp, ffmpeg, ffprobe, and node on PATH.
- This skill loads only after the checkout is trusted: `hermes skills trust` from the project root. Trust is a repo-level decision; do not hand-edit config.yaml.
- Temps stay in the project `tmp/`. Never put media on the host `/tmp`.
- Cookies stay off unless the user explicitly opts in.

Find the project root as the git checkout that contains this skill. Run every CLI command with `terminal` from that root. `uv` is often missing from PATH; call `.venv/bin/scenery-clips` and `.venv/bin/python`.

## Procedure

1. Read `docs/STATUS.md`, `docs/ISSUES.md`, `docs/ARCHITECTURE.md`, and `docs/ROADMAP.md` in that order with `read_file`. Done when you can name what is built, the latest run id, and the open problems.
2. Show the wired vision model and wait. `terminal(command=".venv/bin/scenery-clips vision-show")`. Tell the user the plain sentence it prints, including the model name. Then `clarify` with two choices: continue with that model, or change it. Do not run, rank, label, analyze, export, or download anything until they pick continue. If they want a change, edit `vision.yaml` (backend and model; for an API also base_url and api_key_env), run vision-show again, and ask again. Done when they have agreed to the model named in the latest vision-show output.
3. Check the machine. `terminal(command=".venv/bin/scenery-clips doctor")`. Done when the JSON has `"ok": true`. If it is false, stop and report the missing binary. Do not invent a workaround that skips a gate.
4. Compile the brief before any search. `terminal(command=".venv/bin/scenery-clips explain-prompt \"<prompt>\"")`. Done when the JSON shows theme, min width/height, aspect band, and `n_clips`. No named resolution means 1280×720. Do not continue if you changed resolution, aspect, or N from what the user asked.
5. Search metadata only. `terminal(command=".venv/bin/scenery-clips run --dry-run --prompt \"<prompt>\" --max-results <N> --sleep 2", timeout=300)`. Done when a new `data/runs/<id>/` exists with `candidates.json` and `constraint.json`, and the command's stopped_reason is recorded. `--dry-run` is required. Zero candidates is a stop, not a reason to loosen the brief.
6. Save storyboard tiles. `terminal(command=".venv/bin/scenery-clips rank --run-dir data/runs/<id> --max-videos <N> --max-tiles <N>", timeout=300)`. Done when `ranked.json` exists and tile JPEGs are listed on the ranked rows.
7. Label tiles with the agreed model. Start `terminal(command=".venv/bin/scenery-clips label-tiles --run-dir data/runs/<id> --confirm-vision", background=true, notify=true)` and retain its `session_id`. Do not pass `--confirm-vision` without agreement to that exact model. Wait for completion notification, then read its exit code with `process_manage(action="poll", session_id="<session_id>")`. Done only when it exits 0 and `vision_scores.json` exists.
8. Apply tile labels. `terminal(command=".venv/bin/scenery-clips apply-scores --run-dir data/runs/<id> --scores data/runs/<id>/vision_scores.json")`. Done when the command exits 0 and reports any unmatched IDs.
9. Cut candidate moments. Start `terminal(command=".venv/bin/scenery-clips analyze --run-dir data/runs/<id> --max-videos <N> --max-analysis-s 120", background=true, notify=true)`; retain the session ID and check the final exit code. Done when it exits 0, or exits 1 with failed ranges named. A complete source with no cuts can still exit 0; that is not proof of readiness.
10. Check the run before review. `terminal(command=".venv/bin/scenery-clips verify --run-dir data/runs/<id>", timeout=300)`. Read `ready_for_shortlist` and `n_sources_without_excerpts`. Continue only if ready_for_shortlist is true (verified material exists); disclose empty sources separately.
11. Build the strip packet. Start `terminal(command=".venv/bin/scenery-clips shortlist-review --run-dir data/runs/<id>", background=true, notify=true)`; retain the session ID and check the final exit code. Done when it exits 0 and `review.json` exists. Default is 6 frames including near the endpoints.
12. Label strips with the same agreed model. Start `terminal(command=".venv/bin/scenery-clips label-strips --run-dir data/runs/<id> --confirm-vision", background=true, notify=true)`; retain the session ID and check the final exit code. A mid-moment cut or dissolve must be rejected. A `continuity_suspect` keep needs explicit clearance. Done when it exits 0 and `shortlist_scores.json` exists.
13. Apply strip labels. `terminal(command=".venv/bin/scenery-clips shortlist-apply --run-dir data/runs/<id> --scores data/runs/<id>/shortlist_scores.json")`. Done when it exits 0 and reports selected, excluded, and shortfall counts.
14. Export only when the user asked for files on disk, and only with an explicit allow. Start `terminal(command=".venv/bin/scenery-clips export --run-dir data/runs/<id> --allow-export", background=true, notify=true)`; retain the session ID and check the final exit code. If it exits 2 because `out/<theme>` is owned by another run, use `--theme <new-slug>` without deleting the older folder. Exit 1 records failed moments; do not lower the cap or substitute moments.
15. Prove the export. `terminal(command=".venv/bin/scenery-clips verify --run-dir data/runs/<id> --require-export", timeout=300)`. Done only when `ok` and `export_complete` are true, and the manifest's exported count matches the files in `out/<theme>/clips/`. Report `request_fulfilled` separately: an export can complete while delivering fewer than N.
16. Update `docs/STATUS.md` with the run id, counts, shortfall explanation, and clip paths. Done when it matches the verify JSON and no longer names an older run as the latest.

## Quick Reference

- doctor, vision-show, explain-prompt, run --dry-run, rank, label-tiles --confirm-vision, apply-scores, analyze, verify
- shortlist-review, label-strips --confirm-vision, shortlist-apply, export --allow-export, verify --require-export
- The vision switch is `vision.yaml`. Change backend and model there. Do not put a key in that file.
- Export cap is `export_max_height`, default 720, allowed 720..2160, never scaled.

## Pitfalls

- Long commands (`label-tiles`, `analyze`, `shortlist-review`, `label-strips`, `export`) belong in tracked `terminal(background=true, notify=true)` jobs from the project root. Save each session ID and inspect the final exit code with `process_manage`; do not use sleep-loop polling. The vision commands print no per-image progress, so a running status does not tell you how many labels remain. A foreground tool timeout may leave the CLI process running: check its state before any relaunch, and never infer completion from an artifact's existence alone.
- Do not pass `--confirm-vision` until the user has agreed to the model printed by `vision-show`.
- If the wired call fails, say so and stop. Do not quietly switch to another model or to `vision_analyze`.
- `analyze` defaults to `--max-videos 1`. Pass an explicit count or only one source is cut.
- Analyze spans run concurrently (default 4 workers). Scene detect defaults to `frame_skip=1`; continuity decode width defaults to 160. Legacy: `SCENERY_ANALYZE_WORKERS=1 SCENERY_DETECT_FRAME_SKIP=0 SCENERY_CONTINUITY_DECODE_WIDTH=0`.
- Gate the next stage on `ready_for_shortlist`, not on analyze's exit code alone.
- Export frame counts use full-precision packet PTS against half-open millisecond window endpoints. If a section reports one fewer frame, inspect the raw packet at the right boundary before loosening any gate; never accept a real gap or missing coverage.
- For speed evidence, measure the same ranked tile set with a fixed-latency caller before/after changing concurrency. Report a real run's stage times too, but do not treat runs with different models, cache state, excerpt counts, or interrupted stages as controlled comparisons.
- A shortfall is a result. Do not add weaker clips to hit N.
- Export does not re-trim for continuity. The shortlist interval is what gets encoded.
- The default theme folder is one per theme slug. If an older run already owns `out/<slug>`, export refuses. Pass `--theme` with a new slug. Never delete the other run's clips to make the default name free.
- Video ids may start with `-`. The CLI uses watch URLs; do not pass a bare id to yt-dlp yourself.
- Applying new tile scores makes old excerpts stale. Re-run analyze and verify before review.
- This session's skill list was built at startup. A skill just written here is visible to a new session, or to a fresh `hermes` process whose cwd is this checkout after trust.

## Verification

A delivery is done only when all of these are true:

- `hermes skills list` (or a fresh process with this checkout as cwd, after trust) shows `scenery-clips`.
- `verify --require-export` printed `ok: true` for the run you are reporting.
- The clip files named in `out/<theme>/manifest.json` exist on disk.
- `docs/STATUS.md` names that run id and does not describe a different run as the latest.
