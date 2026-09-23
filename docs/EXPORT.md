# Part 5 — capped export (design)

Status: implemented and live-verified 2026-09-22 (amended 720p-cap contract); design locked after an Astra consult.
Evidence for each done criterion lands in docs/STATUS.md.

Turn a verified run's shortlist into real final clip files: one clip per selected
moment, using the largest native 16:9 video-only rendition at or under the export
cap (default 720p; never below 720p), from SECTION downloads only, with exactly
one controlled encode per clip, and a manifest that reconciles against the
shortlist. Fewer clips than n_clips is reported honestly.

## Position in the pipeline

Parts 1–4 produce `shortlist.json` (selected + excluded + counts) for a run.
Part 5 consumes ONLY that file: no moment that is not `selected` can ever be
downloaded or exported. The default download policy stays forbidden; export is
the single gated path that acquires only the cap-selected sections it planned.


## Approved intervals are sacred

  Export consumes `shortlist.json` selected moments and honors their `start_s` /
  `end_s` exactly. There is **no** continuity re-trim (or any other silent
  window rewrite) at export time — Part 3 already gated continuity, and Part 4
  shortlist is the authorized selection. The encode maps that approved
  half-open interval through the acquisition timeline origin K. If coverage
  cannot be proven for that exact window, the moment fails closed; it is never
  quietly shortened.

## Key decisions (with the changes the consult drove)

1. Acquisition = stream copy, no acquisition-time encode.
   `yt-dlp --download-sections "*<a-2s>-<b+2s>"` (clamped to >= 0 and to the
   source duration), pinned to one resolved video-only format id, with
   `--downloader-args "ffmpeg:-copyts"`.
   We do NOT use `--force-keyframes-at-cuts` for export (it re-encodes at
   acquisition and would make the output a second-generation encode).
   [Consult change: the draft used force-keyframes; rejected as a hidden second encode.]
2. Timeline mapping = absolute timestamps, empirically proven (2026-09-22):
   with copyts, `format.start_time` == the FIRST/MINIMUM video packet PTS == K,
   where K is the absolute source time of the file's local 0. The same source
   window produced the identical K = 966598 ms for both avc1 and av01 720p
   renditions. Frames extracted at the same absolute source times
   (970.0/972.5/975.0 s) had dHash distances 2/1/0 across the two acquisitions.
   The plain (rebased) variant starts at an untraceable content point and is
   rejected. Mapping used everywhere after: `local = source - K`, with K rounded
   to integer milliseconds. If coverage cannot be proven, the moment FAILS CLOSED.
   [Consult change: the draft assumed local offset == span start; the probe proved
   that wrong for stream copy — the "acquire wide" margin exists precisely to
   absorb keyframe snapback on both ends.]
3. Acquisition acceptance (per moment, all required, else the moment fails):
   exactly one video stream and no audio/subtitle/data/other streams; dimensions
   EQUAL the resolved rendition's dimensions; square pixels (SAR 1:1); no
   rotation; SDR + 8-bit only (`yuv420p` or `yuvj420p` — HDR transfers or other
   pixel formats are rejected for v1); full clean decode; and, inside the
   DELIVERED CLIP WINDOW only, the expected frame count and no gap beyond one
   frame. `validate_export_media` calls `probe_export_coverage(path, window_ms=...)`
   and checks its `window_n_frames`/`window_max_gap_s`, not whole-acquisition
   counts. Fragment-boundary holes left by `--download-sections` in the wider
   acquisition margins are explicitly tolerated when they are outside that
   window; live verification found such holes at section tails while delivered
   clip-window content remained continuous. Coverage: `first_pts <= clip_start`
   and `last_pts + frame_duration >= clip_end`.
   `probe_export_coverage` compares the original packet PTS, not a PTS rounded
   to milliseconds, with the half-open millisecond clip window. For example,
   the tree-cutting section had a 33.032833s frame inside an interval ending
   33.033s; integer-millisecond rounding incorrectly counted 131 instead of
   the actual 132. This correction preserves exact expected-frame equality,
   coverage, full decode, and the one-frame max-gap gate; it adds no shortfall
   tolerance and does not alter approved endpoints.
4. Target rendition = the LARGEST advertised video-only rendition at or under
   the export cap. The cap is config key `export_max_height`, default 720, and
   must be an integer from 720 through 2160. The constant floor is 720p.
   Candidates must have strict positive integer dimensions, explicit
   `acodec == "none"`, a native 16:9 aspect (absolute ratio tolerance 0.01),
   and a valid format id. The chosen rendition is never scaled up or down.
   The recorded ranked dimensions are an upper bound: if the best height
   tier exceeds them, the moment fails rather than falling back to a lower
   tier.
   Within the highest eligible height tier, deterministic order is codec
   preference `avc1 > av01 > vp09 > vp9`, then higher fps, then ascending
   format id. A 4K source therefore selects its largest available rendition at
   or below the cap (normally 1280x720 at the default), not a 4K output.
5. Encode = ONE pass, frame-accurate trim on decoded frames:
   `ffmpeg -nostdin -v error -xerror -threads 1 -i ACQ -map 0:v:0 -an -sn -dn
    -vf "trim=start=<a-K>:end=<b-K>,setpts=PTS-STARTPTS"
    -c:v libx264 -preset medium -crf 17 -threads:v 1 -pix_fmt yuv420p
    -fps_mode passthrough -map_metadata -1 -map_chapters -1
    -movflags +faststart <staged>.mp4`
   Interval semantics: half-open [start, end), documented; no forced -r, no
   duplicated frames, no padding. Audio decision for v1: final clips are
   VIDEO-ONLY (-an) — consistent with every other artifact in this project,
   avoids picking up source music/voiceover, and keeps verification strict.
   [Consult: CRF 17 preset medium, single-threaded; one real clip is benchmarked
   in the live pass.]
6. Everything is planned before anything downloads: an immutable schema-2
   `plan.json` (bound to the shortlist hash + generation id, `max_height`,
   per-moment resolved format spec + acquisition span) is written first; the
   export then executes exactly that plan. `fetch_export` accepts only validated
   ExportSpec entries built from the plan (run-scoped authorization:
   `--allow-export` or config `allow_export: true` gates the whole command;
   there is no generic "download any span" entry point).
   [Consult change: plan-first + run-scoped authorization.]
7. Outputs:
   `out/<theme>/` — theme = slug of the constraint's theme_text
   ("beautiful-natural-european-scenery"), overridable with `--theme`.
     plan.json        immutable export plan (schema_version 2; deterministic)
     clips/<video_id>_e<idx>_<start_ms>-<end_ms>.mp4
     manifest.json    the reconciled record (schema_version 2; deterministic;
                      includes max_height; no timestamps)
   A theme directory is owned by one run: an existing manifest from another
   run_id is a hard error (exit 2). Publication is transactional: clips staged
   in `clips/`, fsynced, renamed; manifest replaced atomically; last of all a
   schema-2 pointer `run_dir/export.json` {theme, manifest path, manifest sha256}
   is written — verify discovers exports through it.
   [Consult change: ownership + staged publication + pointer last.]
8. `manifest.json` (schema_version 2) contains: run_id, theme, source constraint
   summary (requested n_clips, geometry), `max_height`, shortlist_sha256,
   generation_id, plan_sha256, export policy + recipe version + toolchain
   versions, per-clip records (identity, source timestamps in ms, acquisition
   span, mapping K, format id/dims/codec, file path relative to out/<theme>,
   size, sha256, acquisition sha256, final dims/duration/frame count), failed
   moments with machine reason codes, and counts: requested, selected, exported,
   failed, `export_complete` (all selected delivered), and `request_fulfilled`
   (n_clips reached) — reported separately, never conflated. The policy is
   `v2-export-cap-sections`; the recipe remains `x264-crf17-medium-v1`.
9. Re-runs are idempotent and safe: acquisitions and clips are reused ONLY if
   the full binding matches (moment identity + interval + acquisition sha256 +
   mapping + resolved format + recipe + toolchain); otherwise rebuilt.
   Deterministic manifest bytes and safe reuse are promised; byte-identical
   encodes across toolchain versions are NOT.
   [Consult: binding-based reuse; no universal bitstream-identity promise.]
10. Failure is honest: any moment that cannot be delivered at cap-compliant
    quality is recorded in `manifest.failed` with a machine-readable reason.
    The contract reasons are:
    `no_rendition_at_target`, `below_720_floor`, `aspect_unsupported`,
    `bad_format_id`, `rendition_exceeds_recorded`, `recorded_dimensions_missing`,
    `metadata_invalid`, `cap_invalid`, `coverage_missing`,
    `coverage_impossible`, `dimension_mismatch`, `quality_band`,
    `duration_mismatch`, `timeline_origin`, `gap_exceeded`, `trim_mismatch`,
    `probe_timeout`, `probe_failed`, `decode_failed`, `download_failed`,
    `encode_failed`, `encode_timeout`, and `size_exceeded`.
    The planner may additionally record `metadata_missing` or `unplannable`;
    the lower-level media validator also uses `streams_invalid`,
    `timestamps_invalid`, `spec_invalid`, `non_square_pixels`,
    `rotation_unsupported`, `hdr_unsupported`, and `bitdepth_unsupported`
    where those specific checks fail. No lower-resolution fallback, no upscaling,
    no substitute moments, no endpoint changes, no padding — ever.
    [Consult: explicit prohibition list + machine-readable codes.]
11. Verify owns proof: with the pointer present, verify schema-validates and
    reconciles the plan and manifest against the re-derived shortlist, then
    checks: manifest bound to the current shortlist hash + generation + plan;
    selected moments partition exactly into clips ∪ failed (disjoint, no
    duplicates); plan dimensions, format ids, and acquisition spans match each
    clip; clip files exist, are not symlinks, carry no unlisted extras, and
    match size + sha256; every clip is in the manifest cap band [720,max_height]
    (the recorded constraint floor is not applied to clips), probes as exactly
    one video stream with no other streams, exact planned dims, native 16:9,
    duration/frames within the interval contract, clean decode, frame count
    and max-gap checks inside the delivered clip window, and the expected
    timeline origin. Fragment-boundary holes in the wider acquisition margin
    are tolerated when outside that window. A present acquisition cache is
    checked for hash, K, and dimensions. Every file in `clips/` is inspected:
    a staging file means publication is in progress or was interrupted; any
    other unlisted file is an error. The verify summary reports
    `acquisitions_checked`. Stale (shortlist changed after export) fails closed
    and names the re-export requirement. `verify --require-export` makes a
    missing export an error; without it, a run that never exported still
    verifies exactly as Parts 1–4 did.
    [Consult: partition checks, cap-band verification, provenance, staging and
    unlisted-file rejection, separate explicit requirement flag.]
12. Exit codes: 0 = every selected moment exported and verified;
    1 = stale inputs, or any per-moment acquisition/encode failure, or the
    shortfall-vs-request gap when standing alone was already true upstream —
    the manifest says which; 2 = invalid invocation, missing authorization,
    malformed run files, theme owned by another run, unsafe paths.

## Safety rails

- Every yt-dlp call: `--ignore-config --js-runtimes node` (existing `_run`),
  plus `--max-filesize` as a hard bound; sections only; no fallback that could
  download more than the planned span. Cookies stay off.
- Sequential on this single-core host; bounded retries (1 per moment);
  per-download and per-encode subprocess timeouts; free-disk preflight and
  all temporary files inside the project.
- Threat model note: sha256 binds artifacts to trusted records; it does not
  authenticate provenance against an attacker who can rewrite everything.

## Implementation review (Astra checkpoint, 2026-09-22) — fixes applied

An independent checkpoint review of the first implementation ("do not proceed
to the live pass yet") drove these corrections before verify/CLI were built:

1. Acquisition is plan-authorized: `fetch_export` takes an `ExportSpec` built
   only from the plan and an explicitly authorized client
   (`allow_export=True`); there is no generic span-download entry point.
2. Coverage is millisecond-precise with NO clamping: a late acquisition start
   fails the moment (`coverage_missing`) instead of silently shortening the
   interval; local times derive from the rounded mapping K used everywhere
   (no float drift between encode and reuse).
3. The acquisition probe proves the mapping contract itself: `format
   start_time` must equal the first packet PTS; duplicate packet timestamps
   fail; frame count and max-gap checks are bounded by one frame interval
   INSIDE THE DELIVERED CLIP WINDOW, not across the whole acquisition. Holes at
   fragment boundaries in the wider acquisition margins are tolerated when
   outside that window; the container's start_time is recorded in the cache
   marker.
4. Reuse binds the acquisition digest: `acq_sha256` is recorded per clip and
   compared on reuse; prior manifests are trusted only through a validated
   pointer (hash-matched); canonical clip names are exact.
5. Exceptions are normalized: probe/decode timeouts become `probe_timeout`,
   encode timeouts become `encode_timeout`, and the retry loop preserves the
   most specific media code plus per-attempt errors.
6. Publication discipline: ownership is checked inside the theme lock, plans
   are written under it, clips and manifest are fsynced, the published
   generation is cleaned of unlisted files, run inputs are re-hashed before
   publishing (changed inputs abort), and the pointer stays last.
7. Inputs are re-validated beyond generation ids: ranked/constraint hashes are
   compared with the analysis manifest, review/labels files must still match
   their bindings; duplicate selected identities and selected/excluded overlap
   are rejected.
8. Spec resolution is hardened: only video-only renditions at or below the
   configured cap, literal format-id allowlist, strict integer dimensions,
   consistent max-dimension choice, recorded-dimension upper bound, native
   16:9 requirement, and explicit pixel-format allowlist. Disk-reserve
   preflight and a post-download size bound were added.

### AMENDED 2026-09-22 — cap decision, source floor, and mapping evidence

The consult amendment changed final delivery from the earlier ≥1080p promise to
an explicit cap contract: default `export_max_height=720`, valid integer caps
720..2160, constant 720p floor, and largest video-only rendition at or below the
cap. The source side changed independently: a prompt with no named resolution
now compiles to a 1280×720 acceptance floor; explicit resolutions still gate to
their dimensions. Aspect band, live/upcoming/auth filters, and storyboard behavior
remain unchanged.

The mapping evidence is specific to both codecs, not an assumption about the
section request: avc1 and av01 720p acquisitions of the same source window had
identical K = 966598 ms, where K is the first/minimum packet PTS and the container
`start_time`; frames at absolute source times 970.0/972.5/975.0 s had dHash
distances 2/1/0. The historical live run `data/runs/20260918T210550Z` retains
its recorded 1920×1080 constraint and 4K sources; amended verify accepts its
720p clips because clips are checked against [720,max_height], not that
recorded constraint floor.

## Done criteria (measurable)

1. `export` CLI exists; `export --help` documents --run-dir/--theme/--allow-export;
   doctor reports the cap-selected export policy; config supports `allow_export`
   and `export_max_height`.
2. Tests (fixtures, no live YouTube) cover: plan uses only selected moments,
   carries schema 2 and `max_height`, and is deterministic; margin clamping at
   0; cap validation and rendition resolution (including the 720p floor,
   deterministic codec/fps/id ordering, and degraded listing failure); copyts
   + pinned-format yt-dlp args; replication of the offset math from source to
   local times (the Part 4 lesson, now with absolute-K mapping); acceptance
   gates (HDR/bitdepth/SAR/dims/coverage); one-encode trim correctness incl.
   half-open semantics; manifest + pointer schema-2 writes; failure recording
   with codes; idempotent re-run byte-identical manifest + reused artifacts;
   stale shortlist fails closed; theme ownership conflict; allow_export gating;
   verify's cap-band and export checks (tamper, missing, extra, counts, stale,
   staging and acquisition provenance) and --require-export.
3. Historical live run `data/runs/20260918T210550Z`: its recorded constraint
   remains 1920×1080 and its sources were 3840×2160; the amended export plan
   uses 720p clips at the default cap, and verify accepts them because the
   clip constraint floor no longer applies. Manifest counts remain honest
   (`export_complete` and `request_fulfilled` are separate).
4. "As envisioned" demonstrations recorded: (a) one clip traced end-to-end
   (shortlist -> plan -> section download -> mapping -> trim -> encode ->
   probe -> verify); (b) a re-run that reuses the acquisition cache and the
   encoded clips and reproduces the same manifest bytes.
5. Docs updated: EXPORT.md (this file), ROADMAP Part 5 -> amended cap contract,
   ARCHITECTURE stage F built, CLI.md (export, verify flags, doctor), DATA.md
   (out/ + cache/export layout), STATUS.md pickup + evidence.
6. Existing tests keep passing — the full suite is green (293 tests after change/tighten); nothing outside
   Part 5 scope changed except the minimal shared edits (yt.py, config.py,
   verify.py, cli.py).
