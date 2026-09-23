# Data layout

All runtime files stay under the project root. `data/` and `tmp/` are gitignored.

  /root/projects/scenery-brief-clips/
    src/scenery_brief_clips/  code (adds brief.py, planner.py to the inherited set)
    tests/                 pytest
    scripts/chain_parts123.py
    docs/                  STATUS, architecture, roadmap, CLI, vision, data
    config.example.yaml    copy to config.yaml, or pass with --config; flat validated defaults
    tmp/                   yt-dlp / HTTP temp (never host /tmp)
    data/
      cache/
        metadata/<video_id>.json     Part 1 player JSON
        storyboards/                 Part 2 sheet JPEGs (hashed URLs)
        tiles/<video_id>/            Part 2 sliced tiles for vision
        analysis/<video_id>_<start_ms>-<end_ms>_<policy>.mp4
                                            Part 3 validated ≤720p video-only span
        analysis/<...>.mp4.complete.json     schema/policy/video/span/size/SHA-256 marker
        analysis/<...>.mp4.lock              per-key concurrency lock
        export/<video_id>_<start_ms>-<end_ms>_<policy>.<format_id>.mp4
                                            Part 5 cap-selected video-only acquisition (copyts absolute timestamps)
                                            default rendition is 1280x720; format id is part of the policy key
        export/<...>.mp4.complete.json       marker schema 2: policy/format/span/probe/mapping/SHA-256
        export/<...>.mp4.lock                per-key concurrency lock
      runs/<UTC timestamp>/
        discovery.json               run-brief sidecar (brief_discovery_v1): brief hash,
                                     query plan, planner provenance, counts, stop reason;
                                     candidates/rejects annotated unverified/metadata_only
        constraint.json
        candidates.json              B keepers
        rejected.json                B rejects
        ranked.json                  C after last apply-scores (or rank if unscored)
        ranked_before_vision.json    backup written once by apply-scores
        vision_scores.json           optional; tile labels
        excerpts.json                D rows: plan, per-range attempts/outcomes, copies, cuts
        analysis_manifest.json        schema/generation + ranked/constraint/excerpts hashes + settings
        review/                       Part 4 frames + strips per candidate moment (safe-id folders, frame_<t_us>.jpg)
        review.json                   Part 4 packet bound to the excerpts generation
        shortlist_scores.json         Part 4 labels written by Hermes/a person
        shortlist.json                Part 4 decision: selected, excluded+reasons, counts, shortfall
        export.json                   Part 5 pointer to the published export (theme, manifest path + sha256)
        .pipeline.lock                serializes rank/apply-scores/analyze/shortlist-*/verify/export for this run
        log.txt

    out/<theme>/                      Part 5 export publication; owned by one run
      plan.json                       immutable per-moment plan (schema 2; max_height,
                                      format spec + acquisition spans)
      clips/<video_id>_e<idx>_<start_ms>-<end_ms>.mp4
                                      final clips (video-only, native 16:9, cap-banded, one encode)
      manifest.json                   schema 2; max_height; reconciled counts, clips,
                                      failures+reasons, hashes, recipe/toolchain
      .export.lock                    per-theme export lock

Video IDs may start with `-`. Cache filenames keep the dash. Always pass a watch URL to yt-dlp, never a bare id.

The export acquisition policy is `v2-export-cap-copyts`; the format-specific cache key is:
`data/cache/export/<safe_video_id>_<acq_start_ms>-<acq_end_ms>_v2-export-cap-copyts.<format_id>.mp4`.
Its completion marker uses schema 2. Export plan and manifest use schema 2 and carry
`max_height`; the run pointer `export.json` uses schema 2. The export policy is
`v2-export-cap-sections` and the unchanged recipe is `x264-crf17-medium-v1`.

Metadata cache is reusable across prompts. ranked.json, vision_scores.json, and excerpts.json are prompt-specific.

Latest delivered live run in THIS project: data/runs/20260923T211413Z — a
run-brief smoke with a frozen plan (metadata-only, 6 candidates, no download;
see docs/STATUS.md). The ocean-wave run below is inherited scenery-clips
history, not run from this fork.
