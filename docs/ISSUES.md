# Current issues and resolved regressions

## Open (2026-09-29): YouTube refuses the host; fallback NOT YET FUNCTIONAL

After ~10 live runs in a few hours YouTube answered every request from this
host with "Sign in to confirm you're not a bot" (still refused at the last
check). Alternate player clients were refused too; it is IP-level. The
runner now stops with status `blocked`, and a circuit breaker stops further
requests in the same process. An opt-in cookies/proxy fallback exists in the
code (config youtube_cookies_file / youtube_proxy) but is NOT YET
FUNCTIONAL: it has never been run against YouTube, it is off, and no
cookies or proxy are configured. Live validation needs the owner.

## Fixed (2026-09-29, B2): a source with no video in a span failed the run

A YouTube source can advertise a longer duration than its video track
(0I1hZCD7sT0: video ends ~129 s of 193 s). A span past the end downloads as
an empty file and one such span failed the whole run, on every rerun. Now
the span is recorded `unavailable` (`no_video_in_span`) when every attempt
comes back with no streams; verify accepts only that exact form, as a
warning. Other acquisition failures still fail the run.

## Fixed (2026-09-29, B2): long clips could never be found

Since step 5 a brief may ask for up to 30 s clips, but analysis windows were
still 9-14 s, so a 16-24 s request produced no excerpts. Windows now have a
minimum length (duration_min + 2 x pad, at least the target), recorded in
the analysis manifest as `min_window_s`.

## Open (2026-09-29, B1 batch): yield and speed limits

- The number of analyzed sources comes from config (`max_analyze_videos`,
  default 1) and does not grow with n_clips: 10 requested clips from 5
  sources gave 8.
- Every excerpt is labeled even when few clips are needed. (Strip labels
  were serial, ~9 s each; since B2 they run 4 at a time, not yet measured.)
- YouTube refused this host after ~10 live runs in a few hours ("Sign in to
  confirm you're not a bot"), metadata included. The runner now stops with
  status `blocked` (wait, then rerun); pacing live runs matters. Cookies
  would bypass it but stay off unless the owner opts in.
- Discovery sleeps 2 s between requests and fetches metadata serially
  (~210 s per run). Since B2 it stops once max_rank_videos candidates are
  kept (the rest were never used); sleeps unchanged because of the YouTube
  block risk.
- verify_export re-decoded analysis copies verify_review already decoded.
  Since B2 the runner reuses those decodes for byte-identical files.
- For geography "european" the planner's queries do not name European
  places; searches returned Canadian lakes (3/6 delivered).

## Mitigated (2026-09-29): legacy continuity gate rejects continuous camera motion

Measured 2026-09-28 on 66 real gate decisions (Brazil run + military
benchmark), blind-labeled from contact sheets, two labels corrected after
frame checks (50 continuous / 16 transitions): the legacy gate keeps 0 of 16
transitions but rejects 25 and trims 9 of 50 continuous single shots, mostly
pans/tracking over marching troops, which is the requested footage.

Fix: `continuity_detector: blend`, the default since 2026-09-29 (owner
approved; `legacy` still selectable; src/scenery_brief_clips/continuity_blend.py). Replayed through the
product code: 0/16 transitions kept, 2/50 continuous rejected (one whip pan,
same window in two runs), 2/50 trimmed. Held-out live run (horses, 22 windows,
never used for tuning): blend 0 errors; legacy kept 3 loop-seam jump cuts and
trimmed 1 continuous shot. Seeded military: candidate excerpts 3 -> 7, still
2/2 delivered. Cost: gate ~+35-90% worker time (+13-16 s per benchmark run),
analyze wall within noise. Margins on the dissolve thresholds are thin and the
held-out set had only hard cuts (no dissolves): watch the first new-theme
runs for dissolves that slip through. Numbers: benchmark/continuity_eval/, benchmark/RESULTS-2026-09-28.md.

## Fixed on this branch: hung analysis downloads piled up and were retried

Live run `data/runs/20260927T220608Z` (Brazilian military, 16 of 20 exported). Full account: `docs/RUN-20260927T220608Z-brazilian-military.md`.

Three faults, all in `yt.py`, not in the shortfall:

1. A 5-minute download timeout was classified as a temporary network error because the message contained "timed out", so a hang was retried.
2. The timeout killed only `yt-dlp`. `ffmpeg` children kept running. New tries stacked on top. This host (1.6 GB RAM) filled memory and swap. The job was stopped on purpose.
3. Analysis calls did not cap `yt-dlp`'s own retry count or socket wait, so one stalled piece could be retried many times inside a single attempt.

The branch stops treating that timeout as retryable, kills the whole download process group, and passes `--retries 1`, `--fragment-retries 1`, and `--socket-timeout 15`. (Superseded 2026-09-28: analysis dropped `--force-keyframes-at-cuts` for stream copy with `-copyts`; see "Resolved: duplicate acquisition" below.)

The 16-of-20 result after the fix is a shortfall, not this bug. One later 403 on `BAx_9MNPzts` was a remote refusal; a cache-reusing retry cleared it.

## Runner reliability pass (2026-09-23/25) — superseded: committed in eedb911

See docs/IMPROVEMENT_PASS.md and benchmark/improvement/RESULTS.md. Local fixes
cover changed-output reuse, interrupted-stage promotion, cached final-verifier
reports, ignored discovery limits, and repeated successful image judgments.
Explicit --live-vision now connects the approved vision wire to the runner.
The final working tree is not yet signed off; historical open issues below
are not all resolved. Live automation worked, but analysis/export remain slow.

As of 2026-09-23. Read this before calling the project ready for a normal order. Fixed history and the latest run evidence are in docs/STATUS.md. An honest shortfall is not itself a defect.

## Resolved 2026-09-28: duplicate acquisition and the analysis re-encode

With the owner's approval, analysis copies are now stream-copied copyts
sections (one yt-dlp call per video) and export adopts them when the rendition
and coverage match; see docs/EXPORT.md amendment and
benchmark/RESULTS-2026-09-28.md. The paragraph below is the history. Latency
still varies on this host; the export encode (now x264 `faster`, recipe
x264-crf17-faster-v1, one thread) is the largest single export cost.

## Open (history): latency varies and duplicate acquisition remains

The older tree-cutting run (`data/runs/20260922T183756Z`) took about 25 minutes from search completion to export completion, including a killed/restarted seven-minute analysis pass. Its 20 tile labels and six strips were sent serially. The new four-clip run (`data/runs/20260922T201824Z`) produced only two reviewable moments; measured stage sum including final verify was 304.45s. Run creation to manifest was 433.87s, including agent pauses. The two runs have different model settings, source/clip counts and cache conditions; the total-time difference is NOT a controlled speedup estimate.

A controlled 20-tile dry-call measurement with 0.12s per call changed from 2.409s serial to 1.216s with a two-call bound (same ranked input and fake caller). Live gpt-6-sol tile labeling took 64.71s for six sources (24 tiles, of which four dark tiles were not sent; 20 calls). The concurrency test proves overlap of exactly two calls and preserved output ordering. Strip calls remain serial; rate limits and variable remote latency may diminish real-world gains. Analysis and export still acquire different section media because analysis uses a cut/re-encode and a ≤720p analysis copy while export requires pinned format and original copyts timestamps. Do not substitute one for the other without proving all coverage/provenance gates.

The new run's 2-of-4 shortfall came upstream: two sources had no usable excerpts and two continuity candidates were rejected; it was not an export failure. Do not weaken those gates or pad the count for speed.

The later ocean run (`data/runs/20260922T205123Z`) reviewed 68 moments, then
excluded 51 near-duplicates; the strip-label stage made one model call per
reviewed moment. A cheaper early candidate filter could reduce calls, but
rejecting moments before visual review might lose the best clean version of
reused footage. Measure quality and shortfall as well as time before changing
selection order. Its overnight pause, foreground timeouts, and inconsistent
stage-duration estimates make it unsuitable as a performance benchmark;
see docs/RUN-20260922T205123Z-ocean-waves.md for corrected counts.

## Fixed: repeated 131-versus-132-frame refusal

Old run `hkBLufo-ZS4` e0 (28.654–33.033s) was refused twice as `timestamps_invalid`. Re-downloading only its approved section to project tmp reproduced it: `probe_export_coverage` reported 131, while raw packet PTS counted 132 frames in the half-open interval, the last at 33.032833s. Integer-millisecond rounding had moved that frame to 33.033s and falsely excluded it. The probe now compares original sub-millisecond PTS to the millisecond endpoints. The same real acquisition passes full decode and validation with exactly 132 in-window frames and maximum in-window gap 0.033367s. Regression: `test_probe_export_coverage_counts_packet_before_end_without_millisecond_rounding`. Exact expected-frame, coverage, gap, resolution, and interval gates remain unchanged. The old run and its three-file manifest were not rewritten.

## Fixed: one empty source hid other reviewable moments

`verify.ready_for_shortlist` now means no integrity errors and at least one verified excerpt. It no longer requires every analyzed source to yield an excerpt. `all_analyzed_sources_have_excerpts` and `n_sources_without_excerpts` report that separate completeness fact; per-source warnings remain. The old run now verifies `ok=true, ready_for_shortlist=true, n_excerpts=6, n_sources_without_excerpts=1`. The new run verifies `ok=true, ready_for_shortlist=true, n_excerpts=2, n_sources_without_excerpts=2`. Regression covers two complete sources, one empty.

## Fixed: contradictory image-labeling instructions

The former base tile prompt unconditionally rejected people as subjects even when the brief requested an activity. The base and brief-specific prompts now say to keep people/machines visibly and centrally doing the requested action, while rejecting incidental people, talking, or unrelated equipment. Generic scenery keeps the people-as-subject rejection. Regressions test both an activity brief and a scenery brief. The active vision.yaml setting is codex-login / gpt-6-sol, explicitly approved for this live test.

## Fixed: prompt count and doctor diagnostic

`4 short clips of trees being cut down` previously compiled as 20 clips with the count text in the theme; it now compiles to n_clips=4 and theme `trees being cut down` (regression). Doctor's 2024 yt-dlp minimum had preceded external JS-runtime support; it now rejects versions before the 2025.11.12 runtime transition (regression), while this host's 2026.08.19 binary passes. Upstream release note: https://github.com/yt-dlp/yt-dlp/releases/tag/2025.11.12

## Open: score provenance and informational constraint fields

`vision_scores.json` stores per-tile labels but not a durable model/backend identity. The CLI reports the active model when it writes scores, and path matching prevents some stale score application, but a future re-label of the same paths with a different model cannot be distinguished from the JSON alone. The current live run used a new run ID and a freshly approved gpt-6-sol wire; no stale-file failure was demonstrated. A provenance schema/migration and policy for manually authored score files require a separate contract; do not claim this has been fixed.

`visual_positives` and `visual_negatives` in constraint JSON are serialized defaults and have no enforcing consumer (the optional, off-by-default Jev gate passes `visual_negatives` to Jev as text context only; see docs/JEV.md). In particular, the listed `people` negative is not an active rejection gate. The actual vision prompt and review labels determine visual matching. These fields should not be presented as enforced constraints; removing or wiring them requires explicit intended semantics and tests.

## Open: no incremental vision-label progress

`label-tiles` and `label-strips` write their score files and print summaries
only after the batch finishes. The ocean run's agent could not see how many
images remained while either command ran. The skill now starts these and other
long stages as tracked background terminal jobs with completion notification;
that prevents a foreground tool timeout from obscuring the job's fate but does
not add per-image progress. A future progress mechanism should report completed,
failed, and total images without changing score-file publication semantics.

## Open: Jev ranking and note check are off by default and thinly validated

`jev_rank`, `jev_note_check` and the manual `jev-gate` (docs/JEV.md) were rebuilt brief-generic on
2026-09-29 and evaluated offline on 184 candidates and 116 vision notes from 10 briefs
(benchmark/jev_eval/README.md). Open:

- Live A/B: only one paired brief so far (R09; benchmark/RESULTS-2026-09-29-jev.md): Jev 2 usable clips vs 3
  without. Causes: the source score ignores Jev's own `place: different` answer (fix proposed: reject when the
  brief names a place and P(place=different) >= 0.5, offline-checked), and an illustration channel passed Jev,
  vision and the note check (note criteria lack "not real camera footage"). Remaining pairs: `benchmark/jev_ab.sh
  R07 R10 R03`. Turning either switch on by default is the owner's decision.
- Thresholds (`jev_reject_below` 0.35, `jev_note_reject_at` 0.70) come from that small set; re-check them on
  new kinds of footage. Probabilities shift when the question wording or state format changes (bump the
  question version).
- Metadata and notes only: no watermark detection, and the note check misses defects the vision note does
  not mention. Sleep/relaxation videos remain the hardest case for metadata judges.

Every failure path falls back (search order, candidate kept, note entry unchanged), so the worst case of
enabling it is a wrongly rejected candidate or moment.

## Operational traps, not logic bugs

- `out/<theme>` can already belong to another run. Use a new `--theme`; never mix or overwrite older files. An earlier tree-cutting export used `stabilize-tree-cutting-20260922-201824`.
- Host `/tmp` is a small memory disk. Pytest is pointed at project `tmp/`; do not lower the 2GB export disk guard.
- Long stages should start as tracked background terminal jobs with notification (see the in-repo skill). A foreground terminal timeout may leave a CLI process running; inspect that process before relaunching. Validated caches allow recovery but do not justify two simultaneous launches.
