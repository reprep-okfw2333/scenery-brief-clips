# Jev: source ranking and vision-note check (optional, off by default)

Jev is TypeSafe's structured decision model (`typesafe/jev-1.13`), served by OpenRouter's
Decisions API (`POST https://openrouter.ai/api/alpha/decisions`, still marked alpha). It takes a
text `state` plus typed `questions` and returns calibrated answers: `noul` (P(true)), `choice`
(one of N, with a probability per option) or `score` (up to 10 ordered levels). It has no image
input and never writes text. Input costs $0.042 per million tokens and output is free. Latency is
about 0.2 s median (p95 about 0.4 s) on this host. Code: `src/scenery_brief_clips/jev.py`.

The project uses Jev in three places, each off unless the config turns it on:

| Switch | Where | What it does |
|---|---|---|
| `jev_rank: true` | run-pipeline, discover stage | orders search hits before any metadata fetch; rejects a candidate after its metadata when the score is at or below `jev_reject_below` |
| `jev_note_check: true` | run-pipeline, label_strips stage | reads each vision `keep` note against the brief; excludes the moment when the note itself reports a failure |
| `jev_gate: true` | manual `jev-gate` command | the same source questions on already-discovered candidates (step-by-step CLI path) |

It only reorders and rejects. It never auto-keeps, so tile labels, vision, continuity, the
shortlist and operator review all still apply.

## Source questions (`jev_source_q_v2`)

One request per search hit or candidate. The state has the brief (request text, subjects,
action, setting, place, exclusions) and the video metadata. Answers:

- `subject` (noul): the brief's subject, the same kind or species, doing the action for a
  meaningful share of the runtime.
- `conditions` (noul): the requested setting, time of day and weather (true if none requested).
- `usable` (noul): worth downloading sections of to find clean 4-30 s clips. Long
  relaxation or ambience films of the real scene count as fine.
- `place` (choice: matches, different, unknown) and `kind` (choice: exterior_scene,
  relaxation_film, vehicle_pov, walking_pov, vlog_or_documentary, dark_or_still_screen,
  compilation_or_slideshow, ai_or_cgi, other). These are recorded for inspection only.

**Score** = mean of P(usable), P(subject), P(conditions). In the offline evaluation this beat
P(usable) alone for every state variant (benchmark/jev_eval/README.md).

### In discovery (`jev_rank`)

1. Search runs as before (the brief's search caps apply unchanged).
2. Every hit is scored from its flat search fields: title, channel, duration, views and the
   description snippet YouTube returns (about 150 characters). Requests run 4 at a time.
3. Metadata is fetched in score order (ties keep search order; an unanswered hit counts as
   0.5). The existing early stop still ends fetching once `max_rank_videos` candidates are kept.
4. After each metadata fetch that passes the rule gate, the candidate is scored again with the
   full metadata (description up to 1200 characters, tags, chapters, categories). If the score
   is at or below `jev_reject_below` (default **0.35**), the candidate goes to rejected.json
   with reason `jev_reject`. It does not count toward the early stop, so fetching continues.
5. Kept candidates are ordered by the full score, so rank's `max_rank_videos` takes the best.

discovery.json gains `jev_rank` (schema `jev_rank_v1`) with the model, question version,
threshold, per-hit and per-candidate scores, compact answers, sources, calls and cost.

### The note check (`jev_note_check`, `jev_note_q_v1`)

After the vision model labels review strips, every entry with `match: keep` is sent to Jev
with the brief and the note (plus scene_type and geo). One noul, `violation`: does the note
itself report a wrong subject or species, wrong action, wrong time of day or weather, a
place that contradicts the brief, mostly people or foreground instead of the subject, a
vehicle window or windshield view, prominent text or watermark, or a cut or title? A note that
only says the place is not recognizable is not a violation.

Each checked entry in shortlist_scores.json gets `note_check: {p_violation, source}`. When
Jev answered, it also gets `note_violation` (true when P(violation) >= `jev_note_reject_at`,
default **0.70**). The shortlist excludes `note_violation: true` with reason
`jev note violation`. Because the flag lives in the labels file, verify reproduces the
shortlist exactly. jev_notes.json records every check. Strip labels supplied with
`--judgments` are not checked.

It only sees what the vision model wrote. A defect the note does not mention (for example
pedestrians described as "street activity") passes.

### The manual gate (`jev-gate`)

`jev-gate --run-dir RUN [--brief BRIEF.json] [--config CFG]` runs after `run`/`run-brief`
and before `rank`. It uses the same source questions and score on cached metadata, rejects at
or below `jev_reject_below`, and orders survivors by score (`jev_order_by_p`). Without
`--brief` it uses the run's theme text. Outputs: jev_gate.json (schema `jev_gate_v2`),
candidates_pre_jev.json (the original list; re-runs gate from it), and candidates.json
rewritten to the survivors. Re-run `rank` if you gate after it. The train-footage questions
(`jev_gate_q_v1`) are gone.

## Config keys (flat YAML, validated)

| Key | Default | Meaning |
|---|---|---|
| `jev_rank` | false | run-pipeline: score and order hits, reject low candidates |
| `jev_note_check` | false | run-pipeline: check vision keep notes |
| `jev_gate` | false | enables the manual `jev-gate` command |
| `jev_reject_below` | 0.35 | reject when score <= this (0..1, below `jev_keep_above`) |
| `jev_note_reject_at` | 0.70 | note violation when P(violation) >= this (0..1) |
| `jev_keep_above` | 0.85 | manual gate only: informational `keep_high` label |
| `jev_order_by_p` | true | manual gate only: order survivors by score |
| `jev_timeout_s` | 20 | per-request timeout |
| `jev_max_usd_per_run` | 0.25 | per-invocation cost cap; once reached, the rest falls back |

Runner bindings: `jev_rank` enters the discover and rank bindings, and `jev_note_check`
enters the label_strips and shortlist_apply bindings, only when switched on. Runs made
without Jev keep their bindings and resume as before. Turning `jev_rank` on for an existing
run re-runs discovery. Turning the note check on re-runs strip labeling from the vision cache
(no new vision calls) and the shortlist. `jev_gate` does not affect the runner.

## Key, fallback and cache

- The key comes only from the `OPENROUTER_API_KEY` environment variable (the same key as the
  GLM wire). Never write it into config, yaml, docs, logs or commits. Jev writes it nowhere.
- run-pipeline passes the live Jev port (`Ports.jev_post`). Tests and other callers without
  that port never reach the network (source `fallback_no_port`).
- No key, HTTP error, timeout, malformed answer, or a spent `jev_max_usd_per_run` fall back
  and record why (`fallback_no_key`, `fallback_error`, `fallback_budget`): search order, the
  candidate kept, the note entry unchanged. Jev being down never fails a run.
- Answers are cached in data/cache/jev/<sha256>.json, keyed by model, questions and state, so a
  rerun is free and repeatable. Probabilities vary slightly between calls (mean |ΔP| of about
  0.015 measured 2026-09-25); the cache pins them.

## Cost

About 1,100-1,500 input tokens per source request and about 560 per note: roughly
$0.00005-0.00007 each. A typical run (20 hits, up to about 10 candidates, about 20 notes) costs
well under $0.01.

## Evidence

Offline evaluation on 184 candidates from 10 briefs and 116 vision notes (2026-09-29, $0.05
of Jev calls): benchmark/jev_eval/README.md. In brief:

- Against a blind Sonnet reference judge, the source score reaches AUC about 0.95. Against
  observed yield (44 analyzed sources) it reaches 0.75-0.80. The reference judge scores the
  same (0.745), so the limit is what metadata reveals, not Jev.
- Replaying the 5 rank slots per brief: search order picked 17 reference-"no" sources out of
  50, and Jev picked 2-3 (black screens, Canadian lakes for "European", fallow or spotted deer,
  explainers).
- The note check agrees with the reference at AUC 0.99. At 0.70 it flags 2 of 5 eye-judged
  bad delivered clips and 1 of 21 good or acceptable ones.
- Live A/B, one paired brief so far (R09 European alpine lakes; benchmark/RESULTS-2026-09-29-jev.md):
  Jev ranked only Alps-titled sources (more excerpts and clips, 4 vs 3). But usable clips were
  2 vs 3: it kept a source it had itself answered `place: different` for (the score ignores
  place), and an illustration channel passed Jev, vision and the note check. Next change:
  a place rule (reject when the brief names a place and P(place=different) >= 0.5; offline it
  rejects 14/184, none truly on-place) and "not real camera footage" in the note criteria.

History: the first gate (2026-09-25, branch exp/jev-gate) used train-specific questions and
P(keep) at 0.40. On 96 hand-labeled train candidates it reached accuracy 0.74 vs 0.49 for rules
alone and AUC 0.88. It ran only as a manual command after all metadata had been fetched. The
runner forced it off.

## Limits

- Metadata and notes only: no pixels, so no watermark detection, and a well-described
  off-brief video can pass while a tersely described good one can score low.
- Sleep and relaxation videos are mixed. A "Sleep … Ocean Sounds" video that gave 2 good
  clips scored low in v1. v2 separates `relaxation_film` from `dark_or_still_screen`.
- Small evaluation set. The thresholds (0.35, 0.70) come from it and have not been re-checked
  on new themes.
- The score ignores the `place` answer (see Evidence), and nothing detects paintings or renders
  from metadata.
- The Decisions API is alpha. The model is pinned (`typesafe/jev-1.13`). Bump the question
  versions when the wording or state format changes, since probabilities shifted by up to
  ±0.1 between state formats.
