# Run log: Brazilian military clips (2026-09-27)

Run id: `data/runs/20260927T220608Z`
Checkout: the copy that matched `origin/master` at `eedb911` when the run started.
This file is the record of what broke, what was a real shortfall, and what was changed locally. It is not a claim that every open issue in `docs/ISSUES.md` is closed.

Nothing in this file is a secret. Model wires name `OPENROUTER_API_KEY` only. The key was never written into the repo.

## What was asked

Gather 20 clips of the Brazilian military, all branches: marches, equipment, speeches. Tone: epic, impressive, recruitment-commercial. At least 720p. The original sentence asked for 2–8 seconds. The form used the only duration band the code accepts, 4/6/12 seconds, after that band was confirmed in the run. Final files are 720p, never scaled. A shortfall is a result, not a reason to pad.

Delivery that passed the project's own check: 16 exported clips, 0 export failures. `verify --require-export` printed `ok: true`, `export_complete: true`, `request_fulfilled: false`, shortfall 4.

Clips: `out/impressive-epic-brazilian-military-all-branches-a-march-mili/clips/`

## Headline

Two different kinds of trouble showed up.

1. The program did not do what its own rules say when a short download hung. That was a bug. It filled this machine's memory and was stopped on purpose.
2. After that bug was fixed, the funnel ran and refused weak footage. That shortfall is the gates working. It is not a bug.

## Bugs found, and the fix on this branch

### 1. A timeout was treated as a reason to try again

`yt.py` classified any error text containing "timed out" or "timeout" as a temporary network glitch. A hard 5-minute download timeout produces that wording, so a hung download was retried instead of recorded as a failure.

Fix: those two markers are no longer transient. `TimeoutExpired` is raised immediately and is not retried. A real server error such as HTTP 429 or 503 can still be retried, and that budget stays small (3 attempts inside `yt.py`, 2 acquisition attempts in analyze).

### 2. Giving up did not stop the download

`subprocess.run` kills only the parent program (`yt-dlp`). The actual downloader (`ffmpeg`) is a child. After the parent was killed, the children kept running and were reparented. The next try started another downloader on top of the old one.

Observed on this host before the fix: about 53–60 minutes still on video 1 of 18, about 16 `ffmpeg` processes, partial files stuck at about 48 bytes, load average about 27, 1.6 GB RAM full, about 4.2 GB pushed into swap. The machine stuttered. The cutter was stopped with SIGTERM. That exit was intentional.

Fix: the default runner starts each download as its own process group and kills the whole group on timeout (TERM, then KILL). One attempt is one process tree.

### 3. The downloader was allowed to retry on its own, and precise cuts made it read the film forward

Analysis calls did not pass `--retries` or `--socket-timeout`. Left alone, `yt-dlp` retries about 10 times and waits a long time on a stalled socket. `--force-keyframes-at-cuts` is still present because the analysis copy needs a cut close to the planned window (duration tolerance is 3 seconds). Combined with a long source, that path made `ffmpeg` read forward through a 23-minute parade to fetch a 14-second piece. Four of those at once, plus leftovers that were never killed, is what filled the machine.

Fix: analysis and export commands now pass `--retries 1`, `--fragment-retries 1`, and `--socket-timeout 15`, so the app's small retry loop is the only real retry budget. The precise-cut flag remains. The process-group kill is what stops a slow precise cut from outliving its attempt.

Tests: `tests/test_yt.py` covers the new command flags and that timeout wording is not transient. `tests/test_vision_wire.py` was updated because this checkout's picture wire is no longer `codex-login` / `gpt-6-sol`. After that update, `tests/test_vision_wire.py` passed 11. The full suite before that one-test update was 390 passed, 1 failed (that stale wire assertion).

## Issues that are not bugs

### Phrase-maker rejected every plan

The checker requires every search phrase to contain one subject name exactly, accents included. "Brazil army" does not count. "Exército" alone does not count.

The first form listed five long official names. The phrase-maker kept paraphrasing, so every plan was thrown out (`subject_mismatch`). The rejected phrases were not saved by the command the other session used, so it could not see what it had proposed.

The form was then reduced to one subject, `Brazilian military`. Branch names stayed in the description only. The phrase-maker still failed. The raw answer, saved under project `tmp/` (not committed), was:

- Brazilian Armed Forces military parade epic
- Forças Armadas do Brasil desfile militar
- Exército Brasileiro Marinha Força Aérea equipamentos
- Brazilian Armed Forces compilation showcase

None of those contains the exact words "Brazilian military". A saved phrase list was used instead. Every line contains those exact words. The checker accepted it. Search then found 18 metadata-eligible videos and rejected 2. Hits are leads, not proof the scene is visible.

### Duration band

The sentence asked for 2–8 seconds. The brief validator accepts only 4/6/12. The run used that band. Clips shorter than 4 seconds were rejected by the continuity gate, which is why many "almost" moments never reached review.

### Honest shortfall: 16 of 20

- 18 videos passed title and size rules. 2 did not.
- Picture labels: 85 keep, 44 reject, 15 uncertain, across 144 tiles. Any one keep sent the video forward, so all 18 went to cutting.
- Cutting, after the fix: 18 complete, 0 range errors on the retry, 26 excerpts. 7 sources had no usable excerpt (title cards, talking heads, compilations with cuts).
- 30 moments were rejected before review because a cut or dissolve sat inside the moment, or the stable piece was shorter than 4 seconds.
- Strip review kept 16, excluded 10: 8 visual rejects, 1 uncertain, 1 duplicate.
- Export: 16 exported, 0 failed. Final check: `ok: true`, `export_complete: true`, 16 acquisitions checked. A sample clip probed as 1280x720, video only.

Do not pad this count.

### One server refusal, then a clean retry

The first fixed cutting pass exited 1. `verify` refused the run because `BAx_9MNPzts` was `partial`: one range around 2236–2250 seconds got HTTP 403 from the video host. That is a real remote refusal, not the hang bug. Completed pieces were cached. A second analyze reused them, fetched the failed piece again, and exited 0 with 18 complete and 0 range errors. `verify` then printed `ready_for_shortlist: true`.

### Hosting the finished files

Litterbox returned HTTP 500 on every upload. 0x0.st returned HTTP 503. Catbox returned "Invalid uploader" for an anonymous upload from this host. A later GoFile guest folder received all 16 files. Guest folders there can disappear. The files on disk in this checkout are the copy that was verified.

## What this branch changes

- `src/scenery_brief_clips/yt.py` — the three download faults above.
- `tests/test_yt.py`, `tests/test_vision_wire.py` — match the new download flags and the OpenRouter picture wire.
- `vision.yaml`, `planner.yaml` — both wires are `openai-api` / `z-ai/glm-5.3-flash` at `https://openrouter.ai/api/v1`, key from `OPENROUTER_API_KEY`. No key in the files.
- `.hermes/skills/scenery-clips/SKILL.md` — follow this checkout, fill the frozen form, use `run-brief`, do not use the older sentence-search path for a new theme.

Not committed: run JSON, caches, clips, tmp, or any key.

## Still true after this run

- Analysis and export still download different media. Do not substitute one for the other.
- Brief exclusions are recorded context, not an automatic visual gate.
- The optional Jev filter stayed off. It is trained on train footage.
- This host has 1.6 GB RAM. Four heavy downloaders at once were too many. The successful rerun used `SCENERY_ANALYZE_WORKERS=2`.
