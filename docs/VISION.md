# What looks at the pictures

vision.yaml, in the project root, is the switch. Edit that file to point labeling at a different login or API. Do not put a key in it.

  backend: codex-login
    The ChatGPT/Codex sign-in already on this machine. Not a paid API key.
    model is the model name. The setting approved for the current live run is gpt-6-sol.

  backend: openai-api
    A normal API call. Also set model, base_url, and api_key_env.
    api_key_env is the name of the environment variable that holds the key.
    base_url can be OpenAI or another service that accepts the same picture call.

Show the current setting:

  .venv/bin/scenery-brief-clips vision-show

Hermes must read that sentence to the user and ask whether to continue with that model or change vision.yaml. Do not start the pipeline, and do not pass --confirm-vision, until the user agrees.

Label commands, after that agreement:

  .venv/bin/scenery-brief-clips label-tiles --run-dir data/runs/<id> --confirm-vision
  .venv/bin/scenery-brief-clips apply-scores --run-dir data/runs/<id> --scores data/runs/<id>/vision_scores.json

Without `--confirm-vision`, labeling refuses and prints the wired model; it
does not look at pictures. `label-tiles` and `label-strips` currently emit no
per-image progress and publish their score files only when the whole batch
succeeds. Use a tracked background job so a terminal timeout is not mistaken
for the underlying process stopping.

Dark tiles marked by the ranker are recorded as reject and are not sent to the model.

# Part 2 tile labels

File shape: { "<video_id>": [ { "path"?, "t_s", "label", "look", "note" } ] }
label: keep | reject | uncertain
keep = the requested outdoor scene or activity is visible and central; for a scenery brief, natural landscape (mountains, water, forest, coast, fields). People or machines actively doing a requested activity are not a generic rejection.
reject = incidental people/conversation/equipment unrelated to the requested activity, maps, title cards, towns/cities as subject, indoor. Generic scenery briefs also reject people as the subject.
Small corner watermarks do not force reject. Two independent tile calls can overlap, but results retain ranked order and failures remain per-image.
look: europe_like | other_landscape | not_nature

Aggregation

  Any keep tile → video promising; windows only around keep tiles (~one tile interval each).
  No keep, some uncertain → video uncertain; windows around uncertain tiles.
  Only reject/dark → video low; no windows.
  Unscored tiles count as uncertain. Prefer exact tile path matching; a pathless t_s score must be within 0.5s of the tile.
  Adjacent selected tiles merge; rejected/unscored gaps are not bridged. Over-budget selected windows are capped inside those windows during analyze.

The scores file is validated before ranked.json changes. Unknown labels exit 2 instead of silently becoming uncertain.
Applying scores makes existing excerpts stale. Re-run analyze, then verify.
