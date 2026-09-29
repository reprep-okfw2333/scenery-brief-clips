# Pipeline runner

The one-command runner is `run-pipeline` (see "Entry" and "Limits" below).
The first sections were written before the runner existed and describe the
stages it chains, the outside systems they touch, and the judgment stops a
person must still make; "What a person has to walk today" is history. The
runner does not replace the stages' rules.

Baseline before any runner edit, in this checkout, from commit 2ce67cc:

    .venv/bin/python -m pytest -q
    358 passed in 174.60s

## What a person has to walk today

There is no single command. An agent or a person runs each command, reads the
result, and decides the next one. The in-repo skill lists that walk. The
commands, in the only order that produces clips, are:

1. doctor — local binaries. Not a clip stage.
2. explain-prompt — compiles a sentence. The brief path does not use it.
3. run --dry-run --prompt, or run-brief --brief --dry-run [--plan]
   Metadata search only. No video. A frozen --plan skips the planner model.
   Without --plan, run-brief makes one planner call.
4. jev-gate — optional, off unless config says jev_gate: true. The runner
   does not call this command; it has its own Jev switches (jev_rank inside
   discover, jev_note_check inside label_strips; docs/JEV.md).
5. rank — storyboard tiles. Needs candidates.json.
6. vision-show — prints the model named in vision.yaml.
7. label-tiles --confirm-vision — refuses without that flag. Writes
   vision_scores.json. Dark tiles are reject and are not sent.
8. apply-scores — deterministic. Needs vision_scores.json.
9. analyze — needs ranked.json. Promising and uncertain rows only. Default
   max videos is 1 unless config or a flag raises it. Writes excerpts.json
   and analysis_manifest.json only after its own checks. Exit 0 with a
   source that has no excerpt is not a failure. Partial or failed ranges
   exit 1.
10. verify — file integrity, not scene truth. ready_for_shortlist means at
    least one verified excerpt and no integrity errors. Do not continue on
    analyze's exit code alone.
11. shortlist-review — frames and strips. Needs a verified run. Uses ffmpeg
    on the analysis copy.
12. label-strips --confirm-vision — same model agreement as tiles. Labels
    only until n_clips distinct keeps exist and skips duplicates of a keep
    (strip label budget, docs/SHORTLIST.md); config `strip_label_budget:
    false` labels every moment. The budget enters the label_strips binding.
13. shortlist-apply — deterministic. Writes shortlist.json. A shortfall is
    a result, not a reason to add clips.
14. export --allow-export — the only video-acquisition path for final clips.
    Refuses without an explicit allow. Default cap 720p, never scaled.
15. verify --require-export — proves the published files.

## Outside systems, and where a fake belongs

| Stage | Code entry | Outside system | Existing injection |
| --- | --- | --- | --- |
| discover | pipeline.run_dry | yt-dlp search and metadata | yt= object with search and fetch_metadata |
| planner | planner.plan_queries | one text model | caller= ; skipped when a frozen plan is passed |
| rank | rank.rank_run | storyboard image fetch | fetcher(url) -> bytes |
| tile labels | vision_wire.label_ranked_tiles | vision model | caller= |
| strip labels | vision_wire.label_review_strips | vision model | caller= |
| analyze | pipeline_analyze.analyze_run | yt-dlp section download, scene detect | fetch_span= (may receive format_id=), prefetch_spans=, detect_fn= |
| review frames | review.review_run | ffmpeg frame extract | no argument; the media call is review._extract_frame |
| export | export.export_run | yt-dlp section + ffmpeg encode/probe | yt= ; encode and probe are inside export.py |
| verify | verify.verify_run | ffprobe / ffmpeg decode | probe_fn=, decode_fn=, export_probe_fn= |

The runner calls those entries. It does not reimplement selection. Tests
substitute fakes at the injection points above. A test must fail if a real
network, model, download, or media tool is reached without that substitution.

Jev: the runner never calls jev_gate.gate_run. With `jev_rank: true` the discover
stage passes a jev.DiscoveryJudge to pipeline.run_dry, and with `jev_note_check: true`
the label_strips stage runs jev.apply_note_check on the fresh strip labels. Both use
the `jev_post=` port; without it no Jev request is made (source `fallback_no_port`).

## Judgment and approval stops

These are the only reasons the runner stops while the run can still continue:

- Vision agreement missing. The stop names the model from vision.yaml
  (backend and model, never a key) and says to resume with vision agreement
  for that exact pair. Labeling does not run before that.
- Tile judgments missing. If no injected tile caller and no captured
  vision_scores are supplied, stop and say so. Do not call the live wire.
- Strip judgments missing. Same rule for shortlist_scores.
- Export allowance missing. Name the flag. Do not download.
- Not ready for shortlist. verify ran, ready_for_shortlist is false. Stop.
  Do not invent excerpts.
- Deadline (added 2026-09-28). Each invocation has a wall-clock budget,
  `run_deadline_s` in config (default 10800). When it is spent the runner
  stops before the next stage with status `deadline` (CLI exit 1); inside
  analyze no new span download starts, and unstarted spans are recorded as
  failed with stage `deadline`. Rerun the same command with the same
  `--run-dir`: completed stages and cached spans are reused, and the new
  invocation gets a fresh budget.
- Blocked (added 2026-09-29). YouTube answered "Sign in to confirm you're
  not a bot" (the host's IP is refused for a while, typically hours) during
  discover, analyze or export. Status `blocked` (CLI exit 1) with a wait-and-
  rerun instruction; the failed stage reruns on the next invocation and
  completed stages are reused. Do not retry in a loop. After the first
  refusal the process stops contacting YouTube (circuit breaker). With the
  opt-in fallback configured (NOT YET FUNCTIONAL: unit-tested only, never
  run live; off by default) (youtube_cookies_file / youtube_proxy) the
  refused call is retried once through it and the rest of the invocation
  uses it; the result then carries `youtube_fallback_used: true`, and a
  `blocked` result says whether the fallback was refused too.
- Interrupted external stage (process killed mid-stage; runner_inflight.json
  left behind). Since 2026-09-29 the next invocation redoes that stage
  automatically, once per stage and run (`auto_recoveries` in the result and
  runner_state.json; counted in timing retries). Every external stage is
  safe to redo: discover/rank/analyze reuse validated caches, the label
  stages their per-image checkpoints, shortlist_review clears each moment
  dir, export clears its staging files and accepts its own run's output. A
  second interruption of the same stage, or config `auto_recover: false`,
  pauses with a recovery instruction as below.
- Uncertain external outcome. A stage finished but its required files do
  not validate, or a completed stage's files changed afterwards. Stop with a
  recovery instruction. Do not call that outside step again until the
  operator acknowledges recovery.
- Another runner holds this run. Stop without writing. The lock file is
  runner.lock inside the run directory, separate from .pipeline.lock so a
  stage can take the stage lock without deadlocking the runner.

A frozen plan makes zero planner calls. Without a plan, the runner calls the
planner only when a planner caller is supplied: `run-pipeline --live-planner`
wires the model named in planner.yaml (planner prompt v2 states the
validator's exact-subject rule; a reply that is not JSON or fails validation
is retried once with the rejection text; network/auth errors are not
retried). discovery.json records the plan and its provenance (model,
model_calls, rejected_attempts). Otherwise the runner stops and asks for
--plan or --live-planner.

Captured judgments and approvals are saved in the run directory and reused
on resume when their binding still matches. A changed vision model cancels
the agreement and the labels that depended on it. Unchanged work does not
ask again.

## What "relevant change" means

This list is the whole rule. Anything not listed does not wipe finished work.
sleep_s, jev_gate and the manual-gate jev_* keys, and SCENERY_ANALYZE_WORKERS
are not selection inputs. jev_rank and jev_note_check (with their thresholds)
are, and enter the bindings only when switched on (table below).

| Change | Invalidates |
| --- | --- |
| brief or frozen plan bytes | discovery and every later stage |
| max_search_results, max_metadata_fetches, min_width, min_height, aspect_min, aspect_max | discovery and every later stage |
| max_rank_videos (derived when config is silent and analyze needs more than 10), max_tiles | rank and every later stage |
| jev_rank on, or jev_reject_below while it is on | discovery and every later stage |
| jev_note_check on, or jev_note_reject_at while it is on | strip labels (re-read from the vision cache), the shortlist, and export |
| vision.yaml backend or model | vision agreement, both label files, and every stage that consumed them (apply-scores onward) |
| max_analyze_videos (or, when config is silent, the count derived from n_clips), max_analysis_s, continuity_* config, SCENERY_DETECT_FRAME_SKIP, SCENERY_CONTINUITY_DECODE_WIDTH | analyze and every later stage |
| export_max_height | export and export verify only |
| dropping export allowance | does not delete earlier stages; export will not run |

The runner stores the binding it used beside each completed stage. Resume
reuses a stage only when that binding still matches and the required files
validate. Completion is recorded only after that validation.

## Required files before a stage counts as done

- discover: candidates.json, constraint.json, and discovery.json when the
  brief path was used. stopped_reason search_error or metadata_errors is a
  failure, not done.
- rank: ranked.json
- label tiles: vision_scores.json, then apply-scores has rewritten ranked.json
- analyze: excerpts.json and analysis_manifest.json, and no partial, failed,
  or invalid video row. A complete row with no excerpt is still done for
  this stage.
- verify before review: verify report ok, and ready_for_shortlist true
  before review starts
- review: review.json
- label strips: shortlist_scores.json
- shortlist: shortlist.json
- export: run export.json pointer and the manifest it names
- export verify: verify --require-export ok

## Retries

The runner adds no retry loop. Existing functions may retry internally
(export already has a bounded attempt loop). An interrupted external stage
is redone once automatically (above). Otherwise, if an outside call's
outcome is uncertain, the runner pauses. The recovery instruction is: inspect the run
directory, fix or delete the partial stage output, then resume with
acknowledge_uncertain for that stage name. Until then the outside call is
not repeated. The retry count in the timing record stays 0 unless a resumed
stage was explicitly acknowledged and run again; that acknowledgement is
recorded as one bounded recovery, not as a hidden repeat.

## Timing record

runner_state.json in the run directory includes a timing object:

- per stage: status executed, reused, paused, or failed; elapsed seconds;
  model_calls; tokens
- active_execution_s: sum of executed and failed stage times
- waiting_for_input_s: time from a pause until the next resume, when the
  saved pause timestamp is present
- retries: recovery reruns only (acknowledged and automatic)
- tokens is null when the caller did not report usage. Null is not zero.

Fake tests prove these fields classify work. They do not prove a speed or
cost saving.

## Entry

    .venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --plan PLAN.json
    .venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --live-planner

Resume with the same command plus --run-dir. Optional flags: --vision-agree,
--live-vision, --live-planner, --planner-config, --allow-export,
--judgments FILE, --acknowledge-uncertain STAGE, --config, --root, --theme.

The in-process function is scenery_brief_clips.runner.advance. The CLI calls
that function. Tests call it too, with fakes passed in. The CLI constructs
live model callers only when asked (--live-vision, --live-planner). Missing
judgments pause.

## Limits

The CLI wires live YouTube search, live vision (--live-vision), the live
planner (--live-planner) and export (--allow-export) only when asked. Fake
tests do not measure speed or token savings; benchmark/bench.sh does. Scene
exclusions stay recorded context, not visual gates.
