# CLAUDE.md

The project rules live in AGENTS.md. Read it first, then HANDOFF.md and
docs/PLAN-PROGRESS-2026-09-28.md. Below are permanent rules learned while
working on this repo (2026-09-28).

## Working rules

- Do not commit, merge or push unless the owner asks.
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
  the labeled set in `benchmark/continuity_eval/` and owner approval.
- ffmpeg contact sheets: put `-t` BEFORE `-i`. With the `tile` filter, an
  output `-t` does not stop input reading.
- Runtime modules load at process start (only planner and jev load lazily), so
  editing src/ while a benchmark runs does not change that run.
