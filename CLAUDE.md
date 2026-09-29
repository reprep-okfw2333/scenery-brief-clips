# CLAUDE.md

The project rules live in AGENTS.md. Read it first, then HANDOFF.md and
docs/PLAN-PROGRESS-2026-09-28.md. Below are permanent rules learned while
working on this repo (2026-09-28/29).

## Working rules

- Do not commit, merge or push unless the owner asks.
- The owner wants the main agent to orchestrate and hand repetitive or costly
  work (blind labeling, writing a test file to a spec, doc audits) to a
  Sonnet sub-agent at high effort. On this host that is the user-level
  `sonnet-high` agent (`~/.claude/agents/sonnet-high.md`, not in the repo);
  elsewhere use an equivalent. Tell it to run only its own test file (1 CPU
  host) and verify what it returns.
- Model wire: vision.yaml and planner.yaml use `openai-api` / `z-ai/glm-5.3-flash`
  on OpenRouter (owner's choice). The key is `OPENROUTER_API_KEY`, kept in
  `~/.hermes/.env`. Load it into a process only; never print it or write it
  anywhere.
- Host has 1 CPU and 1.6 GB RAM. Run one heavy job at a time. Never run pytest
  and a benchmark together. Before timing anything, check `uptime` and `ps` for
  other load (a Hermes agent with headless Chrome has contaminated timings
  before). Leave the long-lived `hermes gateway run` service alone.
- `pkill -f PATTERN` also matches the shell running it when PATTERN appears in
  the command line. Kill by PID instead.

## Measuring

- Use `benchmark/bench.sh` (cold caches, fresh root under tmp/bench/). Compare
  changes with seeded runs (see HANDOFF.md). Live search results drift between
  runs.
- Noise on this host is about ±15% per stage. Report yield (excerpts, clips,
  verify ok) next to timings, never timings alone.
- Before building a heuristic from a plan, test it offline on existing run data
  first (e.g. the keep-ratio gate was contradicted by the Brazil run).

## Media and verification invariants

- Analysis cache policy v4 (`v4-copyts-720`): local time 0 of an analysis copy
  is its first packet PTS (marker `first_pts_ms`, range `mapping_k_s`), NOT the
  span start. Any new code that maps local and source time must use it.
- When bumping a cache policy or export recipe, add the old value to
  `ACCEPTED_ANALYSIS_POLICIES` / `ACCEPTED_RECIPES` so published runs still
  verify. Check this on a real old run, e.g.
  `verify --run-dir data/runs/20260927T220608Z --require-export`.
- The continuity gate is a quality safeguard. Change it only with numbers from
  the labeled set in `benchmark/continuity_eval/` and owner approval. Default
  detector is `blend` (2026-09-29); `continuity_detector: legacy` restores the
  old gate. Confirm gate numbers by replaying the PRODUCT code
  (`benchmark/continuity_eval/replay_product.py`), not only exploratory
  scripts.
- OpenCV 5.0 `phaseCorrelate(a, b, window)` multiplies the window into `a` and
  `b` in place. Pass copies when the arrays are reused.
- Blind contact-sheet labels (sub-agent or human) miss short dissolves and
  loop-seam jump cuts. Where a detector and a label disagree, look at every
  frame before trusting either.
- Never run two pytest processes at once, not even a single test file next to
  the full suite: tests/conftest.py puts every session's temp files under the
  shared project `tmp/pytest/`, and one session removes the other's files
  (seen 2026-09-29: a spurious test_export failure, "source.mp4: No such
  file"). Tell sub-agents to wait or run their file only when no other pytest
  is running.
- pytest collects only classes named `Test*`. A class named `FooTests` is
  silently skipped (tests/test_brief.py lost 29 tests that way until
  2026-09-29). Name new test classes `Test...` or use plain functions.
- ffmpeg contact sheets: put `-t` BEFORE `-i`. With the `tile` filter, an
  output `-t` does not stop input reading.
- Most runtime modules load at process start, but some load lazily inside
  functions: continuity_blend (first continuity scan), jev, and the runner's
  planner import. Do not edit src/ while a benchmark runs.
