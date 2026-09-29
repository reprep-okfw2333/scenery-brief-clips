# Planner evaluation

Live validity of the query planner (planner.yaml wire; 2026-09-29:
`openai-api / z-ai/glm-5.3-flash`). Costs real model calls (small); the key
is read from OPENROUTER_API_KEY or ~/.hermes/.env inside the process only.

The scripts read and write under tmp/planner_eval/:

```bash
.venv/bin/python benchmark/planner_eval/make_briefs.py   # writes tmp/planner_eval/briefs/ (9 briefs)
mkdir -p tmp/planner_eval && cp -r benchmark/planner_eval/briefs_irregular tmp/planner_eval/
TRIALS=3 .venv/bin/python benchmark/planner_eval/eval.py v2          # raw prompt, first attempt only
TRIALS=3 .venv/bin/python benchmark/planner_eval/eval_prod.py        # product plan_queries (prompt + retry)
TRIALS=4 .venv/bin/python benchmark/planner_eval/eval_prod_irr.py    # goose/wolf only
```

`eval.py` compares prompt variants; v1 (the pre-2026-09-29 prompt) is in git
history (brief.py PLANNER_INSTRUCTION before step 4), so today it only runs
the current prompt.

## Results (stored JSON here)

| file | what | result |
|---|---|---|
| results_v1_v2.json | first attempt, 9 briefs x 3 | v1 8/27 valid, v2 25/27 |
| results_prod.json | plan_queries (v2 + one retry) | 27/27 plans, 31 calls |
| results_prod_irregular.json | goose/wolf x 4, final prompt | 8/8 plans, 12 calls |

Note: after results_prod.json the v2 prompt gained one sentence (keep an
irregular noun as written; never invent a plural such as "mouses") because a
retry produced "gooses". PLANNER_INSTRUCTION_VERSION stayed
`search_query_planner_v2`. Only results_prod_irregular.json and the red deer
run (benchmark/RESULTS-2026-09-29-step4.md) used the final text.
