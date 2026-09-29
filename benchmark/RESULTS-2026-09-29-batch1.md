# Batch baseline B1 (2026-09-29)

Eight operator-written briefs (benchmark/batch1/, from benchmark/brief_eval/
after/), run one after another with `benchmark/batch.sh batch1`: live planner
and vision (z-ai/glm-5.3-flash), cold caches, blend continuity, export on, one
fixed config for all (the step 4/5 benchmark config: rank 5 videos x 6
tiles, analyze 5 videos, 90 s analysis per video). Code: 87f17c3 (step 5).
Host quiet (load < 0.4 at each start). Index:
benchmark/runs/batch1-index-20260929T023532Z.tsv; rows and totals:
benchmark/runs/batch1-summary.json (`benchmark/batch_summary.py`).

| id | request | asked | delivered | unattended | wall | model calls |
|---|---|---|---|---|---|---|
| R01 | three cinematic clips of horses grazing in a meadow | 3 | 3 | yes | 20:56 | 55 |
| R03 | short 2-4 second clips of waves crashing, 6 | 6 | 6 | yes | 15:49 | 31 |
| R04 | two clips of the Northern Lights over Norway, ~10 s | 2 | 0 | **no** (analyze failed) | 7:32 | 23 |
| R05 | 10 clips of Tokyo street traffic at night, 8-15 s | 10 | 8 | yes | 26:02 | 39 |
| R07 | four clips of red deer in a misty forest, no people | 4 | 4 | yes | 13:07 | 38 |
| R09 | 6 European alpine lake clips, 5-10 s | 6 | 3 | yes | 17:03 | 40 |
| R10 | two clips of camels walking in the Sahara at sunset | 2 | 2 | yes | 9:54 | 35 |
| R11 | a lighthouse in a storm, three, 20 s long | 3 | 0 | **no** (not ready for shortlist) | 7:11 | 24 |

Totals: 6/8 unattended completions; 26/36 clips delivered; every completed run
verify_export ok; 285 model calls; 7038 stage-seconds. Completed runs average
17 min wall.

Clips by eye: 10 good, 11 acceptable, 5 bad (see "Clip judgement").

## Where the time goes (6 completed runs, 6159 stage-seconds)

| stage | share | avg/run | note |
|---|---|---|---|
| analyze | 26.5% | 272 s | section downloads dominate (prefetch wall), then scene detect, continuity |
| discover | 20.5% | 211 s | serial search + metadata, 2 s sleep between requests |
| label_strips | 12.6% | 130 s | one model call per excerpt, serial (~9 s each); all excerpts labeled even when n_clips is small (R01: 25 excerpts for 3 clips) |
| export | 11.5% | 118 s | x264 encode on 1 CPU, ~4.4 s per output second at 720p (R05: 92 s of clips -> 402 s); all 720p clips adopted the analysis copy |
| label_tiles | 9.2% | 94 s | serial model calls |
| shortlist_review | 7.4% | 76 s | review frames per excerpt |
| verify_export | 6.3% | 65 s | re-decodes every analysis copy verify_review already decoded, plus the clips |
| verify_review | 5.4% | 56 s | strict decode of every analysis copy |

## Failures and shortfalls, with causes

- **R04 (bug, blocks unattended runs):** video 0I1hZCD7sT0 reports 192.9 s but
  its video track ends at ~128.8 s (audio continues). A storyboard tile at
  151 s still showed a picture, so a span 151.6-157.6 s was planned; yt-dlp
  "succeeds" with an empty 261-byte file (no streams), with or without
  -copyts (reproduced 2026-09-29, tmp/repro-r04). The acquisition check
  rejects it correctly, but ONE failed span among 10 fails the whole run
  (runner.py: any partial/failed row -> analyze failed), and a rerun fails the
  same way.
- **R11 (bug, step 5 regression):** the brief asks for 16-24 s clips, but
  analysis windows come from kept storyboard tiles (tile interval + 2 s pad
  each side: 8.9 s and 14 s here). No window can hold a 16 s clip, so 0
  excerpts and verify_review "not ready for shortlist". Only 2 of 5 ranked
  sources had kept tiles.
- **R05 shortfall 8/10:** 5 analyzed sources gave 14 moments; 3 duplicates of
  one shot and 3 visual rejects left 8. The analyzed-source count is fixed
  by config regardless of n_clips (and defaults to 1 with no config).
- **R09 shortfall 3/6:** search returned mostly Canadian Rockies lakes (Moraine
  Lake etc.); vision correctly marked them geo conflicting/uncertain for
  "european". The planner's queries did not name European places. Uncertain
  moments are excluded.

## Clip judgement

Contact sheets (1 fps; 12 frames for clips over 12 s) of all 26 delivered
clips, judged against the request text by a Sonnet sub-agent, with ffmpeg
scene-score scans for cuts; labels in benchmark/batch1/judge/labels.json.
Two "bad" verdicts re-checked by the orchestrator on the sheets (R10 camels
in daylight from a car window; R07 fallow deer in a misty field with a
name watermark): both confirmed.

| run | clips | good | acceptable | bad |
|---|---|---|---|---|
| R01 horses | 3 | 2 | 1 | 0 |
| R03 waves | 6 | 2 | 4 | 0 |
| R05 Tokyo traffic | 8 | 2 | 3 | 3 |
| R07 red deer | 4 | 1 | 2 | 1 |
| R09 alpine lakes | 3 | 2 | 1 | 0 |
| R10 camels | 2 | 1 | 0 | 1 |
| total | 26 | 10 | 11 | 5 |

- 21/26 (81%) usable (good or acceptable); 5 bad: two Tokyo clips that are
  mostly pedestrians/foreground heads with a watermark, one through a rainy
  windshield with a wiper; the fallow deer; the daytime camels.
- No cut or dissolve inside any delivered clip (continuity gate held).
- Watermarks/logos are the most common flaw: 4 of 6 wave clips carry a
  corner site logo (counted acceptable), plus 3 more clips. Owner has
  deprioritized watermark detection.
- Against the requests as asked: 21 usable of 36 requested clips (58%).

## Reruns of the two failures on the fixed code (2026-09-29, later)

`benchmark/batch.sh batch1 R04 R11` after the B2 fixes (min analysis window,
unavailable spans); 697 tests passing.

- R04 (benchmark/runs/batch1-R04-20260929T112307Z): 2/2 delivered, verify
  ok, unattended, 12:59, 28 model calls. Search drifted: the broken source
  0I1hZCD7sT0 was not among the candidates, so the unavailable-span path was
  not exercised live (covered by the reproduction and unit tests).
- R11 (benchmark/runs/batch1-R11-20260929T113607Z): windows were now 20 s
  (the fix works: 3 + 4 ranges of 20 s, one 11 s video whole), but EVERY
  download failed with YouTube's "Sign in to confirm you're not a bot":
  after ~10 live runs in a few hours YouTube refused this host (metadata
  lookups too, checked minutes later). Run status "failed" at analyze. Not a
  pipeline bug, but a run-level condition an unattended orchestrator must
  recognise: the runner now reports it as status `blocked` with "wait and
  rerun the same command" (cookies stay off unless the owner opts in).
  Live validation is paused until the block lifts.
