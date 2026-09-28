---
name: scenery-clips
description: Use when making scenery clips from a frozen brief.
version: 0.2.0
author: Luigi Manzi (reprep-okfw2333), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    editorial_name: Scenery Clips
    editorial_description: Turn a theme into checked YouTube clips through the frozen brief and the two OpenRouter model wires.
    tags: [YouTube, Scenery, Clips, Pipeline]
    requires_tools: [terminal, process_manage, read_file, write_file]
    requires_toolsets: []
    requires_plugins: []
---

# Scenery Clips

This skill is for the scenery-brief-clips checkout that contains this file, and only when that checkout's HEAD equals origin/master after a fetch. It is not the original scenery-clips project. Older copies of this skill live in other folders; do not follow them. Their folders are untrusted on purpose.

Turn one theme into short on-disk clips. Fill a frozen form, let the planner wire propose search phrases once, then walk the existing stages. Search hits are leads, not proof the scene is visible. If fewer than N pass, return what passed and why. Never pad, and never quietly lower resolution, aspect, or N.

## When to Use

- The user wants scenery clips, a scenery shortlist, or the next stage of a run in this checkout.
- A run dir already exists and the next stage is the ask.

Don't use for: the original scenery-clips tree, a behind copy of this repo, general YouTube download, or full-film rips. Do not start a new pipeline part until the user explicitly says to.

## Prerequisites

- Linux. Run locks use fcntl.
- This checkout, with `.venv/bin/scenery-brief-clips`, yt-dlp, ffmpeg, ffprobe, and node on PATH.
- Trust is already granted for this checkout. Do not trust the older project folders to "load their skill."
- Temps stay in the project `tmp/`. Never put media on the host `/tmp`.
- Cookies stay off unless the user explicitly opts in.
- Two model wires, both already set, neither holding a key:
  - `vision.yaml` looks at pictures (tiles, then strips).
  - `planner.yaml` receives the frozen form and proposes search phrases once.
  - Both: `backend: openai-api`, `model: z-ai/glm-5.3-flash`, `base_url: https://openrouter.ai/api/v1`, `api_key_env: OPENROUTER_API_KEY`.
- Before any live model command, export `OPENROUTER_API_KEY` from the Hermes env file if the process environment is empty. Never print the key. Never write it into yaml, docs, logs, or chat.
- The local video-cutting workers are not a model. The Jev filter is a different hardcoded model and stays off unless the user asks for it.

Find the project root as the git checkout that contains this skill. Run every CLI command with `terminal` from that root. `uv` is often missing from PATH; call `.venv/bin/scenery-brief-clips` and `.venv/bin/python`.

## Procedure

1. Confirm this checkout. `terminal(command="git fetch origin && git rev-parse HEAD && git rev-parse origin/master")`. Done when the two SHAs match. If they do not, stop. Do not work in another folder with a similar name.
2. Read `docs/STATUS.md`, then `docs/ISSUES.md`, `docs/ARCHITECTURE.md`, and the schema section of `docs/SEARCH_BRIEF.md`. Ignore that file's "not implemented" header; STATUS says the frozen brief and `run-brief` are built. Done when you can name what is built and the open problems.
3. Show the picture wire. `terminal(command=".venv/bin/scenery-brief-clips vision-show")`. Done when it prints API `https://openrouter.ai/api/v1`, model `z-ai/glm-5.3-flash`, key from `OPENROUTER_API_KEY`. That pair is the agreed wire. Do not ask again, and do not pass `--confirm-vision` for a different pair. If the sentence names anything else, stop and show it.
4. Check the machine. `terminal(command=".venv/bin/scenery-brief-clips doctor")`. Done when the JSON has `"ok": true`. If it is false, stop and report the missing binary.
5. Fill one `search_brief_v1` form from the user's words. Shape: `benchmark/improvement/brief.json`. Required fields and provenance rules: `docs/SEARCH_BRIEF.md`. Write it inside this project, not on the host `/tmp`. Ask only about a missing clip count or a real contradiction. Do not invent exclusions or an unstated count. Do not use `explain-prompt` or `run --prompt` for a new theme. Done when the file validates, or when you have stopped to ask the one missing fact.
6. Search metadata only, through the planner wire. Export the key first if the environment is empty, then `terminal(command=".venv/bin/scenery-brief-clips run-brief --brief <brief.json> --dry-run", timeout=300)`. Do not pass `--plan` unless a frozen plan file already exists. Done when a new `data/runs/<id>/` has `candidates.json`, `constraint.json`, and `discovery.json`, and the command's stopped_reason is recorded. Zero candidates or an empty plan is a stop, not a reason to loosen the brief. Every hit is `visual_status=unverified`.
7. Skip `jev-gate` unless the user asked for it. Do not turn it on.
8. Save storyboard tiles. `terminal(command=".venv/bin/scenery-brief-clips rank --run-dir data/runs/<id> --max-videos <N> --max-tiles <N>", timeout=300)`. Done when `ranked.json` exists and tile JPEGs are listed on the ranked rows.
9. Label tiles with the agreed wire. Start `terminal(command=".venv/bin/scenery-brief-clips label-tiles --run-dir data/runs/<id> --confirm-vision", background=true, notify=true)` and retain its session id. Wait for the completion notification, then `process_manage(action="poll", session_id="<id>")`. Done only when it exits 0 and `vision_scores.json` exists.
10. Apply tile labels. `terminal(command=".venv/bin/scenery-brief-clips apply-scores --run-dir data/runs/<id> --scores data/runs/<id>/vision_scores.json")`. Done when it exits 0. Re-run analyze if excerpts already exist; new scores make them stale.
11. Cut candidate moments. Start `terminal(command=".venv/bin/scenery-brief-clips analyze --run-dir data/runs/<id> --max-videos <N> --max-analysis-s 120", background=true, notify=true)`. Pass an explicit video count; the default is 1. Done when it exits 0, or exits 1 with failed ranges named. Exit 0 with no cuts is not proof of readiness.
12. Check the run before review. `terminal(command=".venv/bin/scenery-brief-clips verify --run-dir data/runs/<id>", timeout=300)`. Continue only if `ready_for_shortlist` is true. Disclose sources with no excerpts separately.
13. Build the strip packet. Start `terminal(command=".venv/bin/scenery-brief-clips shortlist-review --run-dir data/runs/<id>", background=true, notify=true)`. Done when it exits 0 and `review.json` exists.
14. Label strips with the same agreed wire. Start `terminal(command=".venv/bin/scenery-brief-clips label-strips --run-dir data/runs/<id> --confirm-vision", background=true, notify=true)`. A mid-moment cut or dissolve must be rejected. Done when it exits 0 and `shortlist_scores.json` exists.
15. Apply strip labels. `terminal(command=".venv/bin/scenery-brief-clips shortlist-apply --run-dir data/runs/<id> --scores data/runs/<id>/shortlist_scores.json")`. Done when it exits 0 and reports selected, excluded, and shortfall counts. A shortfall stays a shortfall.
16. Export only when the user asked for files, and only with an explicit allow. Start `terminal(command=".venv/bin/scenery-brief-clips export --run-dir data/runs/<id> --allow-export", background=true, notify=true)`. If `out/<theme>` is owned by another run, use `--theme <new-slug>`. Do not delete the older folder. Do not lower the cap.
17. Prove the export. `terminal(command=".venv/bin/scenery-brief-clips verify --run-dir data/runs/<id> --require-export", timeout=300)`. Done only when `ok` and `export_complete` are true, and the manifest count matches the files in `out/<theme>/clips/`. Report `request_fulfilled` separately.
18. Update `docs/STATUS.md` with the run id, counts, shortfall explanation, and clip paths. Done when it matches the verify JSON and no longer names an older run as the latest. Commit only if the user asks.

## Quick Reference

- doctor, vision-show, run-brief --brief --dry-run, rank, label-tiles --confirm-vision, apply-scores, analyze, verify
- shortlist-review, label-strips --confirm-vision, shortlist-apply, export --allow-export, verify --require-export
- Picture wire: `vision.yaml`. Search-phrase wire: `planner.yaml`. Both are GLM 5.3 Flash on OpenRouter. No key in either file.
- Do not use `explain-prompt` or `run --prompt` for a new theme.
- Jev stays off. Export cap default 720, never scaled.

## Pitfalls

- A same-named skill in another checkout is an earlier build. Those folders are untrusted. Do not trust them to load a skill.
- `docs/SEARCH_BRIEF.md` still opens with "not implemented." The implemented contract is in `docs/STATUS.md`. Follow STATUS.
- Long commands belong in tracked `terminal(background=true, notify=true)` jobs. Check the final exit code with `process_manage`. Do not infer completion from a file existing.
- If the wired call fails, say so and stop. Do not switch models or label pictures yourself.
- `analyze` defaults to `--max-videos 1`. Pass an explicit count.
- Gate the next stage on `ready_for_shortlist`, not on analyze's exit code alone.
- A shortfall is a result. Do not add weaker clips to hit N.
- Brief exclusions are recorded context, not an automatic visual gate. Say that before delivering files.
- Export does not re-trim for continuity. The shortlist interval is what gets encoded.
- If an older run already owns `out/<slug>`, pass a new `--theme`. Never delete the other run's clips.
- Video ids may start with `-`. The CLI uses watch URLs.
- This session's skill list was built at startup. A skill just written here is visible to a new session started in this checkout.

## Verification

A delivery is done only when all of these are true:

- This checkout's HEAD equals origin/master, and the command used was `.venv/bin/scenery-brief-clips`.
- `vision-show` named `z-ai/glm-5.3-flash` at `https://openrouter.ai/api/v1`.
- Discovery came from `run-brief`, and `discovery.json` records the planner wire. It did not come from `run --prompt`.
- `verify --require-export` printed `ok: true` for the run you are reporting, if files were requested.
- The clip files named in `out/<theme>/manifest.json` exist on disk.
- `docs/STATUS.md` names that run id and does not describe a different run as the latest.
