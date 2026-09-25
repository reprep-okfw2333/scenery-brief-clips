# Improvement pass: observed results

Live run: data/runs/20260925T180736Z. Invocation and raw timing: benchmark/improvement/live-first.json. Frozen brief/plan and config are adjacent.

The single command completed from discovery through final verification without agent stage handoffs. Four metadata candidates, two ranked/analyzed sources, seven reviewed moments, two selected, five duplicates excluded. Two exports, zero failed, no shortfall. Recorded final verify: ok=true, errors=[], warnings=[], export_complete=true, request_fulfilled=true. Read back both files and probed: one video stream, 1280x720 each, durations 11.400 and 11.411 seconds.

Active stage sum: 1210.54 seconds (20.18 minutes). Analysis: 674.05 seconds (11.23 minutes); export: 273.69 seconds (4.56 minutes). Fourteen visual model calls (7 tiles + 7 strips), zero planner calls; token usage/billing unknown. Quality was judged by the approved gpt-6-sol wire; no additional independent playback review by the parent. File verification is not scene certification.

This proves automatic progression on one live request, NOT a speedup. The run started before later checkpoint/cap/prompt-binding corrections, so it does not constitute an end-to-end acceptance test of the entire final working tree. Full-suite log: 388 passed in 346.34 seconds. Two further regressions were added afterward (image mutation during checkpoint hit; changed prompt invalidates runner labels) and passed in focused suites. The subsequent pre-commit full suite passed all 390 tests in 182.71 seconds.

Implemented locally: explicit --live-vision; completed-output checksums and stage checkpoints; conservative interrupted-stage recovery; final verifier re-execution; stricter configured discovery caps/geometry; per-image successful-judgment checkpoints with image/prompt/wire identity and locks; prompt-aware runner invalidation. Failed judgments are never checkpoint hits. Checkpoint-reuse evidence is currently offline, not measured live token savings.

Incomplete: live replay on final code, complete runner/CLI/vision documentation synchronization, broad independent code review, and controlled speed comparison. This is a user-requested progress snapshot, not a completed improvement pass.

Clips:
- out/improvement-ocean-two/clips/PYux6w2zkqo_e0_300-11697.mp4
- out/improvement-ocean-two/clips/vPhg6sc1Mk4_e0_300-11694.mp4
