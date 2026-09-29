# batch1: batch baseline (plan step, 2026-09-29)

Purpose: measure unattended completion, clips delivered versus requested, wall time and model calls across varied, mostly generic requests. The briefs were written by an operator agent from docs/SEARCH_BRIEF.md and live in benchmark/brief_eval/after; the files here are byte-for-byte copies. All runs use one fixed config, the step 4/5 benchmark config (benchmark/step5-iceland/config.yaml), and the live planner (no plan.json). Run with `benchmark/batch.sh batch1`, summarize with `python benchmark/batch_summary.py <index.tsv>`.

| ID | request_text | n_clips | duration band | export cap | geography |
|---|---|---|---|---|---|
| R01 | three cinematic clips of horses grazing in a meadow | 3 | 4-12 s (target 6) | 720p | none |
| R03 | short 2-4 second clips of waves crashing, I need 6 of them | 6 | 2-4 s (target 3) | 720p | none |
| R04 | two clips of the Northern Lights over Norway, around 10 seconds each | 2 | 8-12 s (target 10) | 720p | Norway |
| R05 | 10 clips of Tokyo street traffic at night, 8-15 seconds | 10 | 8-15 s (target 11.5) | 720p | Tokyo |
| R07 | four clips of red deer in a misty forest, no people | 4 | 4-12 s (target 6) | 720p | none |
| R09 | 6 European alpine lake clips, 5-10 seconds, 720p | 6 | 5-10 s (target 7.5) | 720p | european |
| R10 | two clips of camels walking in the Sahara at sunset | 2 | 4-12 s (target 6) | 720p | Sahara |
| R11 | clips of a lighthouse in a storm, three of them, 20 seconds long | 3 | 16-24 s (target 20) | 720p | none |
