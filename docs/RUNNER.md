# Pipeline runner

This file was written before the runner existed. It records the stages already
in the code, the outside systems they touch, and the judgment stops a person
must still make. The runner chains those stages. It does not replace their
rules.

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
   does not call this. It stays off even if that config key is true.
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
12. label-strips --confirm-vision — same model agreement as tiles.
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
| analyze | pipeline_analyze.analyze_run | yt-dlp section download, scene detect | fetch_span=, detect_fn= |
| review frames | review.review_run | ffmpeg frame extract | no argument; the media call is review._extract_frame |
| export | export.export_run | yt-dlp section + ffmpeg encode/probe | yt= ; encode and probe are inside export.py |
| verify | verify.verify_run | ffprobe / ffmpeg decode | probe_fn=, decode_fn=, export_probe_fn= |

The runner calls those entries. It does not reimplement selection. Tests
substitute fakes at the injection points above. A test must fail if a real
network, model, download, or media tool is reached without that substitution.

Jev is not in this table on purpose. The runner never calls jev_gate.gate_run.

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
- Uncertain external outcome. A stage was started and its required files do
  not validate. Stop with a recovery instruction. Do not call that outside
  step again until the operator acknowledges recovery.
- Another runner holds this run. Stop without writing. The lock file is
  runner.lock inside the run directory, separate from .pipeline.lock so a
  stage can take the stage lock without deadlocking the runner.

A frozen plan makes zero planner calls. Without a plan, the runner calls the
planner only if an injected planner caller is supplied. Otherwise it stops
and asks for a plan file. It does not call the live planner wire.

Captured judgments and approvals are saved in the run directory and reused
on resume when their binding still matches. A changed vision model cancels
the agreement and the labels that depended on it. Unchanged work does not
ask again.

## What "relevant change" means

This list is the whole rule. Anything not listed does not wipe finished work.
sleep_s, jev_* keys, and SCENERY_ANALYZE_WORKERS are not selection inputs.
Jev keys are ignored because the runner never runs that gate.

| Change | Invalidates |
| --- | --- |
| brief or frozen plan bytes | discovery and every later stage |
| max_search_results, max_metadata_fetches, min_width, min_height, aspect_min, aspect_max | discovery and every later stage |
| max_rank_videos, max_tiles | rank and every later stage |
| vision.yaml backend or model | vision agreement, both label files, and every stage that consumed them (apply-scores onward) |
| max_analyze_videos, max_analysis_s, continuity_* config, SCENERY_DETECT_FRAME_SKIP, SCENERY_CONTINUITY_DECODE_WIDTH | analyze and every later stage |
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
(export already has a bounded attempt loop). If an outside call's outcome is
uncertain, the runner pauses. The recovery instruction is: inspect the run
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
- retries: recovery reruns only
- tokens is null when the caller did not report usage. Null is not zero.

Fake tests prove these fields classify work. They do not prove a speed or
cost saving.

## Entry

    .venv/bin/scenery-brief-clips run-pipeline --brief BRIEF.json --plan PLAN.json

Resume with the same command plus --run-dir. Optional flags: --vision-agree,
--allow-export, --judgments FILE, --acknowledge-uncertain STAGE, --config,
--root, --theme.

The in-process function is scenery_brief_clips.runner.advance. The CLI calls
that function. Tests call it too, with fakes passed in. The CLI does not
construct a live model caller. Missing judgments pause.

## Not this milestone

No live YouTube search, live vision call, live export, or claim that fake
tests measured speed or token savings. Scene exclusions stay recorded
context, not new visual gates. Quality, continuity, and shortfall rules are
unchanged.
