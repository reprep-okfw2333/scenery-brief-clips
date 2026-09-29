# Step 4 (planner) results, 2026-09-29

Live planner wire `openai-api / z-ai/glm-5.3-flash` (OpenRouter). Data and
scripts: benchmark/planner_eval/.

| measure | result |
|---|---|
| prompt v1, first attempt valid (9 briefs x 3) | 8/27 (all failures subject_mismatch) |
| prompt v2, first attempt valid | 25/27 |
| production path (v2 + one retry) | 27/27 plans, 31 calls |
| irregular plurals after prompt fix (goose, wolf x 4) | 8/8 plans, 12 calls, no invented plurals |

## One-command live run on a new theme (red deer in a misty forest)

`BENCH_INPUTS=step4-reddeer BENCH_LIVE_PLANNER=1 benchmark/bench.sh step4-reddeer`
= `run-pipeline --brief ... --live-planner --vision-agree --live-vision
--allow-export`, cold caches, blend continuity default.

- completed, 5:27 wall, no operator input after the command; 28 model calls.
- planner: valid plan on the first call (7.3 s): "red deer walking through a
  misty forest", "red deer walking in foggy forest", "red deer in misty forest
  footage", "red deer misty forest compilation".
- 9 candidates, 4 ranked, 5 ranges, 3 excerpts, 0 continuity rejects,
  2/2 clips at 1280x720, verify_export ok.
- Stages (s): discover 111, label_tiles 71, analyze 58, review 22, strips 19,
  export 33, verify_export 11.
- Operator check of the delivered clips (4 fps sheets): both single continuous
  shots, no dissolves. d7iNPctM-Qg: red deer stag in golden mist, but a
  "nature" watermark in the corner. zfTTwVZazAo: deer in a misty forest that
  looks like a white-tailed deer, possibly rendered/AI imagery; vision kept
  it. Both are vision-stage quality gaps (plan step 7), not planner or gate.
