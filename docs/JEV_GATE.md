# Optional Jev metadata gate (`jev-gate`)

An experimental, **optional** pre-filter. It is **off by default**. When enabled it runs **after
discovery (`run` / `run-brief`) and before `rank` / tile review**. It never downloads video, never looks
at pictures, and never replaces tile review or vision.

## What it does

For each candidate in `candidates.json` (each one has already passed the rule-based metadata gate:
resolution, aspect, live/availability), it sends one request to TypeSafe's Jev decision model:

- Model: `typesafe/jev-1.13` (pinned). It is a structured decision model, not an LLM, and has no image input.
- Endpoint: OpenRouter Decisions API, `POST https://openrouter.ai/api/alpha/decisions`.
- State: the run theme (`constraint.json` `theme_text`, plus `visual_negatives` and "cab views" as things to avoid) plus cached metadata only:
  title, channel, follower count, duration, views, likes, upload date, best resolution/fps, up to 25 tags,
  and the description cut to 1200 characters.
- Questions (one request per candidate, question version `jev_gate_q_v1`): `live_action`, `shot_type`,
  `on_brief`, `branding`, `compilation`, `cinematic`, and `keep`. Only **P(keep)** drives the decision;
  the other answers are recorded for inspection. The question wording is fixed in
  `src/scenery_brief_clips/jev_gate.py` and currently written for train footage (see Limitations).

Decision per candidate:

| P(keep) | Decision | Effect |
|---|---|---|
| ≤ `jev_reject_below` (default **0.40**) | `reject` | removed from `candidates.json` |
| between the thresholds | `keep_review` | kept; goes to rank / tile review as usual |
| ≥ `jev_keep_above` (default 0.85) | `keep_high` | kept; **label only**, it still goes to rank / tile review |
| no answer (fallback) | `keep_fallback` | kept (the rule gate already accepted it) |

**It rejects only; it never auto-keeps.** `keep_high` does not skip tile review, vision, or any later
gate. Survivors are ordered by P(keep), highest first (`jev_order_by_p: true`), so `rank --max-videos`
and `analyze --max-videos` caps are spent on the most promising candidates first. Fallback candidates keep
discovery order after the scored ones.

## Enabling it

1. Set `jev_gate: true` in `config.yaml`, or in a file passed with `--config` (it must sit inside the project root).
2. Put an OpenRouter API key in the environment as `OPENROUTER_API_KEY`. The key is read **only** from
   the environment. Never put it in config.yaml, vision.yaml, planner.yaml, or any other file; the gate
   never writes it to artifacts or error messages.
3. Run the gate right after discovery and before `rank`:

       .venv/bin/scenery-brief-clips run-brief --brief brief.json --dry-run --plan plan.json
       .venv/bin/scenery-brief-clips jev-gate --run-dir data/runs/<id> --config config.yaml
       .venv/bin/scenery-brief-clips rank --run-dir data/runs/<id> --config config.yaml ...

With `jev_gate` unset or false, `jev-gate` prints `"enabled": false`, exits 0, and changes nothing.
If you re-run the gate after `rank`, run `rank` again: rank reads `candidates.json`.

## Config keys (flat YAML, validated)

| Key | Default | Meaning |
|---|---|---|
| `jev_gate` | `false` | master switch (boolean) |
| `jev_reject_below` | `0.40` | reject when P(keep) ≤ this (0..1; must be below `jev_keep_above`) |
| `jev_keep_above` | `0.85` | informational `keep_high` label only (0..1) |
| `jev_timeout_s` | `20` | per-request timeout in seconds (> 0) |
| `jev_max_usd_per_run` | `0.25` | per-run cost cap, summed from the `usage.cost` of each response; once reached, remaining uncached candidates fall back (> 0) |
| `jev_order_by_p` | `true` | order survivors by P(keep) |

## Fallback to the rule gate

When a candidate gets no usable Jev answer, it is **kept**, exactly as the rule gate alone would have kept
it. The reason is recorded in `jev_gate.json` as `source`:

- `rules_fallback_no_key`: `OPENROUTER_API_KEY` is not set (no request is made)
- `rules_fallback_error`: HTTP error, timeout, transport error, or a malformed or missing `keep` probability
- `rules_fallback_budget`: the per-run cost cap was reached
- `rules_fallback_no_metadata`: no cached metadata for that video

## Outputs and cache

- `data/runs/<id>/jev_gate.json` (schema `jev_gate_v1`): model, question version, thresholds, theme, counts
  by decision and source, total `cost_usd`, and per-candidate `p_keep`, compact sub-answers with
  probabilities, `decision`, `source`, `cache_hit`, `latency_s`, `cost_usd`.
- `data/runs/<id>/candidates_pre_jev.json`: the original candidate list, written once. Re-runs re-gate
  from this file, so a changed threshold can bring a candidate back.
- `data/runs/<id>/candidates.json`: rewritten to the survivors. rank, analyze, and verify read it as before.
- `data/cache/jev/<sha256>.json`: cached decisions keyed by model + question version + questions + state.
  A cache hit makes no request and costs nothing.

## Cost and latency

Jev bills input tokens only. About 1,560 input tokens per candidate, roughly **$0.07 per 1,000 candidates**
(about $0.001 for a 15-candidate run). Median latency was about 0.18 s per request (p95 0.25 s). Requests
are sequential, so a 15-candidate run adds a few seconds.

## Evaluation (2026-09-25, offline, train-footage briefs)

Measured on 96 hand-labeled candidates (36 keep, 60 reject) from six train-footage searches:

| | Rule gate only | Rules + Jev (reject ≤ 0.40) |
|---|---|---|
| Accuracy | 0.49 | 0.74 |
| Reject precision | 1.00 | 0.97 |
| Reject recall | 0.18 | 0.60 |

- AUC of P(keep) was 0.88. Low scores are reliable (1 keep among 38 candidates below 0.5); high scores are
  not (50–75% keep), which is why there is no auto-keep.
- Main catches the rules miss: AI/CGI, cab-view and ambience videos, model trains, vlogs and documentaries,
  re-uploads.
- A replay on the six runs had about 27–31% fewer candidates reaching tile review, but only about 2–3% less
  analyze download, because manual tile review already removed most of the waste.
- The live A/B rerun could not be completed: YouTube's bot check blocked downloads from the test host.

## Limitations

- It **cannot detect watermarks** or burned-in text from metadata; tile review and vision stay mandatory.
- The 0.40 cutoff was **tuned on train footage only**, with labels from one reviewer. Re-check it before
  relying on it for other themes, and after any change to the questions or state format (probabilities
  shifted by up to ±0.1 between two state formats).
- The question set (`jev_gate_q_v1`) mentions trains, trackside vs cab views, and model railways. It is
  meant for train-footage briefs; for other themes the questions would need rewording (new question
  version) and a fresh evaluation.
- It judges titles and descriptions, not pixels: a well-described off-brief video can pass, and a terse
  good video can score low.
- Probabilities vary slightly between calls (mean |ΔP| ≈ 0.015); the cache makes re-runs deterministic.
