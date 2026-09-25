# Current issues and resolved regressions

As of 2026-09-23. Read this before calling the project ready for a normal order. Fixed history and the latest run evidence are in docs/STATUS.md. An honest shortfall is not itself a defect.

## Open: latency varies and duplicate acquisition remains

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

`visual_positives` and `visual_negatives` in constraint JSON are serialized defaults and have no enforcing consumer (the optional, off-by-default Jev gate passes `visual_negatives` to Jev as text context only; see docs/JEV_GATE.md). In particular, the listed `people` negative is not an active rejection gate. The actual vision prompt and review labels determine visual matching. These fields should not be presented as enforced constraints; removing or wiring them requires explicit intended semantics and tests.

## Open: no incremental vision-label progress

`label-tiles` and `label-strips` write their score files and print summaries
only after the batch finishes. The ocean run's agent could not see how many
images remained while either command ran. The skill now starts these and other
long stages as tracked background terminal jobs with completion notification;
that prevents a foreground tool timeout from obscuring the job's fate but does
not add per-image progress. A future progress mechanism should report completed,
failed, and total images without changing score-file publication semantics.

## Open: Jev gate is evaluated offline on train footage only

The optional `jev-gate` (off by default; docs/JEV_GATE.md) was evaluated on 96 hand-labeled train-footage
candidates labeled by one reviewer (accuracy 0.74 vs 0.49 for rules only; reject precision 0.97; AUC 0.88).
Not yet shown:

- A live A/B run through analyze. The attempt on 2026-09-25 was blocked by YouTube's "Sign in to confirm
  you're not a bot" check on the test host, and cookies are off by policy.
- Any theme other than trains. The 0.40 cutoff and the question wording (`jev_gate_q_v1`) are tuned for
  train footage; P(keep) shifted by up to ±0.1 when the state format changed.
- Watermark or burned-in text detection. Jev sees metadata only, so tile review and vision stay mandatory.

It never auto-keeps, and every failure path falls back to the rule gate, so the worst case of enabling it is
a wrongly rejected candidate (in the offline set, 1 of the 38 candidates scoring below 0.5 was a true keep).

## Operational traps, not logic bugs

- `out/<theme>` can already belong to another run. Use a new `--theme`; never mix or overwrite older files. An earlier tree-cutting export used `stabilize-tree-cutting-20260922-201824`.
- Host `/tmp` is a small memory disk. Pytest is pointed at project `tmp/`; do not lower the 2GB export disk guard.
- Long stages should start as tracked background terminal jobs with notification (see the in-repo skill). A foreground terminal timeout may leave a CLI process running; inspect that process before relaunching. Validated caches allow recovery but do not justify two simultaneous launches.
