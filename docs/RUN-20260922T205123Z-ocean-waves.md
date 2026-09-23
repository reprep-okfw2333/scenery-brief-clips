# Run report: ocean-waves-crashing-against-rocky-cliffs (run 20260922T205123Z)

End-to-end log of the scenery-clips pipeline run, from kickoff to delivered links.
Written by Hermes after the run. UTC artifact timestamps are directly observable;
reported stage durations are rough agent estimates unless a start and end were
recorded. Do not use them as a controlled performance benchmark.

## Request

- Brief: four short, 16:9, 720p clips of ocean waves crashing against rocky
  cliffs, separate files.
- Authorizations given up front by the user: use gpt-6-sol through the ChatGPT
  sign-in to judge pictures; download the final selected video sections; no
  further approvals.
- One mid-run user correction ("stop") killed the very first attempt before any
  bytes were downloaded; the run below started after the user pointed the job
  at the scenery-clips project and skill.

## Setup and checks

- Loaded the youtube-clip-sourcing skill and the project's AGENTS.md /
  docs/STATUS.md first, as the skill requires.
- `.venv/bin/scenery-clips doctor`: clean. No warnings. Python 3.14.4,
  yt-dlp 2026.08.19, ffmpeg 8.0.1, node 26.8.2 all present.
- `.venv/bin/scenery-clips vision-show`: vision switch already set to
  backend codex-login, model gpt-6-sol — exactly the model the user had just
  authorized, so no config edit was needed and no key was touched.
- Stated the model to the user (skill rule) before any labeling; the user's
  standing authorization was the "yes".

## Stage-by-stage log

### 1. Search + metadata gate (run --dry-run) — ~1 min
20:51 UTC. 4 query variants (plain, 4k, compilation, drone), 20 max results,
2 s sleep between extractor calls. Output: 15 candidates kept, 4 rejected
(3 wrong aspect, 1 below min resolution), stop reason "complete". This was
sufficient to rank 8 sources for a request of 4 clips; it did not guarantee
that 4 usable moments would pass later gates.

### 2. Storyboard rank
8 of the 15 candidates got tiles sliced (max-videos 8, max-tiles 12); all 8
ranked "uncertain" pending vision labels. No errors. ranked.json was later
rewritten by apply-scores, so its final mtime is not the ranking completion time.

### 3. Tile vision labels (label-tiles --confirm-vision) — background
Started as a tracked background job. vision_scores.json is dated 20:55:59 UTC
and contains scores for all 8 videos, with zero reported errors. The earlier
~9-minute duration estimate does not reconcile with the run start at 20:51
UTC and is not a reliable measurement. The command produced no progress output
while running; process state and artifact timestamps were the only useful
status signals.

### 4. apply-scores — seconds
All 8 videos moved uncertain → promising. No unmatched score IDs. This is the
point where the brief's visual gate existed: without labels every keeper stays
uncertain, per the skill.

### 5. Shot analysis (analyze, max-videos 8, max-analysis-s 120) — ~73 min elapsed
Approximately 21:04–22:17 UTC, including a foreground timeout and relaunch.
It acquired 120-second-capped ≤720p video-only analysis windows (557 MB into
data/cache/analysis), then ran PySceneDetect and the continuity gate. This
elapsed interval is not a measurement of uninterrupted processing time. Two
operational notes:

- First attempt was launched in the foreground; the terminal tool timed out at
  its 420 s cap. The process behind it survived and kept running, but the
  tool lost track of it. A background invocation was then started with
  completion notification; the second invocation found a partly warm cache.
  The available record does not prove whether the two processes overlapped.
  The old skill did not yet prescribe background execution.
- While waiting I polled with sleep-loops; two of those sleep commands were
  themselves killed by tool timeouts / an interruption signal. Neither touched
  the pipeline. The "interruption" the user saw was one of these sleep timers,
  not the analysis job.

Result: excerpts.json — 7 of 8 sources produced usable excerpts (68 total
candidate moments: b7EzXFveBtk 12, 97bDawihFDc 12, xvNNTc6ZPtQ 11,
m3v9Rj7-QAk 11, bAtvCJmwzMg 11, Oh_Kx01eUV4 10, jGiBBq7dBpk 1; ygHl19aXliQ
produced 0). 5 candidate excerpts were rejected upstream by the continuity gate
(stable subspans 1.5–3.6 s, all below the 4 s duration minimum) — evidence the
Part 3 gate was actually cutting things, not rubber-stamping.

### 6. Temporal review (shortlist-review) — ~13 min, background
First foreground attempt timed out at 300 s (again the foreground cap); the
process survived and continued. A background invocation with notification
then completed with 68 moments, 408 frames (6 per moment), 0 errors, and
review.json. The skill now advises starting this stage in the background.

### 7. Strip vision labels (label-strips --confirm-vision) — ~7 min, background
One call per moment strip through gpt-6-sol. Wrote shortlist_scores.json,
0 errors. Same no-progress-output quirk as stage 3.

### 8. Shortlist (shortlist-apply) — seconds
Deterministic collapse: 68 candidate moments → 4 selected. Exclusions, with
a reason for each:
- 9 beyond n_clips (surplus after diversity spread)
- 51 near-duplicates (deduplicated against eligible moments; a referenced
  winner can itself later be excluded for exceeding the quota). The largest
  clusters had 9 duplicates each of Oh_Kx01eUV4:0 and xvNNTc6ZPtQ:1, and 8 of
  bAtvCJmwzMg:0 — long compilation loops showing the same breakers.
- 4 visual match rejected (the strip showed something off-brief)
- upstream: 5 continuity rejects already counted above
- 1 source (ygHl19aXliQ) contributed no moments at all.
Selected 4, from 4 different sources, all labeled coast, request_fulfilled
true, shortfall 0:
- 97bDawihFDc  328.5–337.9 s
- Oh_Kx01eUV4  338.5–349.9 s
- bAtvCJmwzMg  328.4–337.8 s
- m3v9Rj7-QAk  0.8–11.2 s

### 9. Export (export --allow-export) — ~7 min, background
This is the step the user pre-authorized. It resolved one literal 720p
video-only rendition per moment (never re-evaluating a selector at download
time), downloaded exactly those 4 short sections, and made exactly one
libx264 encode per clip. Finished clean: requested 4 / selected 4 / exported
4 / failed 0 / export_complete true / request_fulfilled true. Files landed
in out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/ with
manifest.json (source video id + absolute timestamps per clip).

### 10. Verify (verify --require-export) — ~1 min
The repo's fail-closed verifier passed: "ok": true, zero errors — input
hashes, planned-range reconciliation, timeline coverage (K ≤ clip start,
last-frame end ≥ clip end), probe, duration, strict decode, export checks.

### 11. Independent spot-check — ~1 min
ffprobe on each file: all 1280×720, durations 9.40 / 9.41 / 10.31 / 11.40 s.
One frame extracted from each and stacked into a 4-up image, inspected: all
four show waves against rocky coasts, no people, no titles, no artifacts.

### 12. Upload (user asked afterward) — ~1 min
Litterbox (litterbox.catbox.moe), 72-hour expiry, no account, per the skill's
sharing recipe. 3 of 4 uploads succeeded first try; one hit a transient DNS
resolution failure and succeeded on retry. All 4 returned URLs verified with
HTTP HEAD (all 200) before being handed over:
- ujf1rf.mp4 (m3v9Rj7-QAk, bright daylight breakers)
- 03j1l6.mp4 (97bDawihFDc, rocky point with mountains)
- dv0hyr.mp4 (bAtvCJmwzMg, storm surf, heavy sky)
- y7e0zs.mp4 (Oh_Kx01eUV4, spray against a dark cliff)

## Operational lessons and documented gates

What happened in this run (the skill has since been updated):
- Long stages in the foreground: the old skill showed foreground examples,
  and analyze and shortlist-review hit tool-level timeouts before subsequent
  attempts were launched as tracked background jobs. A terminal timeout did
  not reliably kill the child process; check whether it is still running
  before relaunching. The skill now documents background execution from the
  start, completion notification, and reading the final process result.
- Vision-call progress: label-tiles and label-strips emitted no incremental
  output, so a running process could not report how many pictures remained.
- The run spanned 20:51 UTC to about 12:01 UTC the next day, but this is not
  active processing time. The overnight interval between excerpts.json
  (22:17) and review.json (11:42) includes a pause; tool timeouts and agent
  waits are mixed into the rest. There is no reliable per-stage benchmark
  for this run.
- Shortlist must give a reason for every exclusion: documented, worked
  exactly as described — 64 exclusions, each with a reason, shortfall honest.
- verify as the acceptance gate: documented, passed fail-closed on the first
  run, including the export checks.
- Export exit 0 ≠ delivery: the docs warn a shortlist with nothing selected
  can still "export" green. Not hit here (4/4 selected), but the manifest +
  verify + independent decode were checked anyway.

Friction not covered by the skill at the time:
- label-tiles / label-strips print nothing until they finish; a tracked process
  confirms they are still running, but stdout cannot show per-image progress.
- One transient DNS failure on the upload host (not a pipeline stage).
- The tool-level timeout on the first analyze launch left a running process
  that the terminal tool could no longer track. The later invocation finished
  with cached media, but overlap was not established; avoid a blind relaunch.

## Issues encountered (all resolved)

1. First run abandoned after user "stop" — nothing had been downloaded; no
   cleanup needed.
2. Foreground timeout on analyze; process survived, relaunched in background,
   analysis cache made the relaunch incremental.
3. Foreground timeout on shortlist-review; same pattern, recovered.
4. Two sleep-based polls killed by tool timeouts / signal; no effect on the
   pipeline.
5. DNS blip on one upload; retried successfully.

## Final tally

- 15 search hits → 8 vision-gated sources → 68 candidate moments →
  4 delivered clips from 4 different sources. The shortlist excluded 64 moments:
  51 duplicates, 9 beyond the quota, and 4 visual rejects; 5 continuity rejects
  occurred upstream and are not part of those 68 moments.
- All delivered: 1280×720, 16:9, 9.4–11.4 s, H.264, verified by the repo
  verifier and by independent ffprobe + frame inspection.
- 0 failed exports; no selected clips left undelivered; no manual override
  of a quality gate. Process overlap during recovery was not established.

Files:
- Clips: out/ocean-waves-crashing-against-rocky-cliffs-no-people/clips/ (4 mp4s)
- Manifest: out/ocean-waves-crashing-against-rocky-cliffs-no-people/manifest.json
- Run record: data/runs/20260922T205123Z/
- This report: docs/RUN-20260922T205123Z-ocean-waves.md
