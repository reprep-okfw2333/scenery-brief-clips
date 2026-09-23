# Part 4 — shortlist (design)

Status: implemented and live-verified 2026-09-21 (CLI: shortlist-review / shortlist-apply; verify checks shortlist.json when present). Evidence in docs/STATUS.md.

Turn an analyzed, verified run into a final selection: up to n_clips moments,
or a truthful shortfall. The CLI prepares review material; vision stays outside
the Python process (same pattern as Part 2). Every decision is recorded.

## Inputs

  run dir (from analyze + verify): constraint.json, ranked.json, excerpts.json,
  analysis_manifest.json. The analysis generation must still be valid: ranked,
  constraint, and excerpts hashes must match the manifest, and the referenced
  ≤720p copies must already satisfy Part 3 rules.

## Outputs (all inside the run dir)

  review/<safe video_id>/<excerpt_index>/frame_<t_us>.jpg  sampled frames per moment
  review/<safe video_id>/<excerpt_index>/strip.jpg         frames joined left→right
  review.json          packet: moments + frame paths/timestamps + signatures
  shortlist.json       the decision document (deterministic; no timestamps)
  shortlist_scores.json  written by Hermes/a person (labels)

  Folder names use the same safe-id rule as the analysis cache
  (analysis_cache.safe_video_id: non [A-Za-z0-9_-] characters become "_", an
  8-hex suffix is appended when anything changed, empty becomes "missing-id").
  Review material is written only under run_dir/review (plus an explicit
  containment check), so a crafted video_id in excerpts.json cannot escape the
  run dir. Frame names are microsecond-stamped (frame_<t_us>.jpg) and a
  duplicate name for one moment is an error, so counts always match files.

## Commands

  shortlist-review --run-dir DIR [--frames N>=2]
    Extracts frames across each candidate moment from its cached ≤720p copy
    (not one still). Default --frames 6 (was 4). Samples always include
    near-start and near-end (50ms inset) plus evenly spaced interior points,
    so a dissolve in the last second cannot hide past the final sample.
    Writes review/ + review.json, bound to the excerpts generation hash.
    Per-moment errors are recorded; any failure exits 1. Frame timestamps in
    review.json are source times; extraction maps each sample into the copy's
    own timeline via the moment's analysis_span offset. When strip endpoints
    diverge strongly (dHash or mean-RGB), the moment is flagged
    continuity_suspect. The run dir must already exist (a missing --run-dir
    exits 2 without creating anything).

  shortlist-apply --run-dir DIR --scores DIR/shortlist_scores.json
    Validates labels, recomputes the decision, writes shortlist.json.
    Malformed labels exit 2 without writing. Stale run/review exits 1.
    n_clips in constraint.json must be a real integer (exit 2 otherwise).
    Unreadable or malformed run files (constraint/ranked/excerpts/manifest/
    review) exit 1 with a clear message, never a traceback.

## Labels file (shortlist_scores.json)

  { "<video_id>": [ { "excerpt_index": 0,
                      "match": "keep" | "reject" | "uncertain",
                      "geo":   "supported" | "uncertain" | "conflicting",
                      "scene_type": "mountains",        # free text, grouped
                      "note": "..." } ] }

  excerpt_index refers to the position in that row's excerpts list.
  Unknown video_id, out-of-range index, duplicate entries, or unknown
  match/geo values are rejected. A moment with no entry is unscored.

  The labels file must live inside the run dir (convention:
  DIR/shortlist_scores.json). shortlist-apply refuses paths outside it, and
  shortlist.json records the labels path relative to the run dir, so the
  binding stays valid when the run directory is moved.

  Optional generation binding: a top-level
  "excerpts_sha256": "<64-char lowercase hex>" key may accompany the
  per-video entries. When present it must equal the current excerpts.json
  hash; otherwise shortlist-apply exits 2 ("labels were written for a
  different analysis generation") and verify reports the mismatch. This is
  the safe way to keep a labels file across a re-analysis.

## External vision labeling rubric (strips)

  Label from the strip (left→right = time), not a single still. Reject the
  moment when the strip shows a cut, dissolve, or scene change mid-moment —
  even if most frames match the brief. Part 3 now also runs a continuity gate
  before excerpts.json is published (trim or reject unstable windows); vision
  remains defense-in-depth for dissolves the gate under-thresholds and for
  non-continuity rejects (titles, people, city). Prefer match=reject with a
  short note naming the discontinuity when the strip is mixed.


## continuity_suspect clearance

  `shortlist-review` may mark a moment `continuity_suspect` when strip endpoints
  diverge (defense in depth on top of the Part 3 gate). That flag is **not** an
  auto-reject of pans: it forces a closer look.

  On `shortlist-apply`, if a moment is `continuity_suspect` and the label is
  `match=keep` **without** an explicit clearance, the moment is excluded with
  reason `continuity_suspect_uncleared` (treated like uncertain — it cannot pass
  unnoticed). If the label is already `reject` or `uncertain`, that label stands.

  Clear a suspect keep with either:
  - `"note": "continuity_ok: …"` (prefix, case-insensitive; rest of the note is free text), or
  - `"continuity_ok": true` (optional boolean on the label entry).

  Example:

  ```json
  {
    "excerpt_index": 0,
    "match": "keep",
    "geo": "supported",
    "scene_type": "fjord",
    "note": "continuity_ok: slow aerial pan, endpoints match the same ridge line"
  }
  ```

## Decision rules (per moment, in this order)

  1. unscored                       -> excluded "no label"
  2. match == "reject"              -> excluded "visual match rejected"
  3. match == "uncertain"           -> excluded "visual match uncertain"
  4. geo == "conflicting"           -> excluded "geographic evidence conflicting"
  5. match == "keep" + geo in
     {supported, uncertain}         -> eligible; geo uncertain carries
                                       flags: ["geo_uncertain"]
  Conflicting geography is never selected as scenery, even with match keep.

## Dedup (near-identical collapse)

  Each moment's frames get a 64-bit difference hash (dHash). Two eligible
  moments are duplicates when the smallest Hamming distance between any of
  their frames is <= DEDUP_HAMMING_MAX (10). Deterministic order is by
  (video_id, excerpt_index); the first eligible moment wins, later duplicates
  are excluded with "duplicate of <video_id>:<index>". No cross-source or
  same-source special cases: reused footage collapses wherever it appears.
  A winner can itself lose the quota race ("beyond n_clips"); the duplicate's
  reference still names it and the checker accepts that case.

## Diversity (after dedup)

  Greedy round-robin over scene_type groups (group order = first appearance
  in deterministic order). Within a group, prefer the moment from the source
  (video_id) with the fewest picks so far; ties break by deterministic order.
  Stops at n_clips. Remaining eligible moments are excluded with
  "beyond n_clips" (only when the quota is already met).

## Shortfall contract

  shortlist.json stores n_clips_requested top-level; counts holds n_candidates,
  n_sources_without_moments, n_selected, n_excluded, n_excluded_by_reason,
  request_fulfilled, n_continuity_rejected_upstream, and
  n_continuity_rejected_upstream_by_reason.
  shortfall = max(0, requested - selected); request_fulfilled is selected >= requested.
  When shortfall > 0, "explanation" states requested/selected/shortfall/
  request_fulfilled and enumerates each shortlist exclusion reason with its
  count. It also surfaces **upstream** Part 3 continuity rejects (moments that
  never became shortlist candidates) with counts/reasons from analyze's
  continuity_rejected lists — so a trees-style 2-of-3 shortfall explains the
  continuity losses, not only apply-time exclusions. The CLI shortlist-apply
  summary prints the same fields. The system never loosens resolution, aspect,
  or quality, never auto-widens search, and never pads with rejected material;
  a smaller honest selection is the correct outcome.

## Checker contract (verify)

  When shortlist.json exists, verify additionally requires:
  - run inputs unchanged since analysis (existing checks) and review.json /
    shortlist.json bound to the same excerpts generation hash (review.json's
    own excerpts_sha256 field is checked against the manifest);
  - labels file present and resolving inside the run dir (relative bindings are
    resolved against it), with its hash equal to the one recorded in shortlist;
  - when the labels payload carries a top-level excerpts_sha256, it equals the
    manifest's excerpts hash (labels bound to a different generation fail);
  - review frame signatures are non-empty per moment (an empty list is stale,
    never a silent dedup exemption);
  - deterministic re-derivation from (excerpts, review signatures, labels,
    n_clips) reproduces the stored selected/excluded lists exactly (this is
    also how "selection <= eligible" is enforced: any hand-edited selection of
    an ineligible moment surfaces as a re-derivation mismatch);
  - every selected moment references an excerpt whose cache key exists in the
    verified copies; every non-selected moment has at least one reason; each
    "duplicate of <video_id>:<index>" reference names an analyzed moment that
    is not itself a duplicate (the referenced moment may itself be excluded,
    e.g. "beyond n_clips");
  - review.json frame_hashes are well-formed (list of 16-character hex strings);
    non-object rows/excerpts, non-list fields, or malformed run files make
    shortlist-review/shortlist-apply exit 1 with a clear message, never a
    traceback, and never a partial shortlist.json.
  - counts reconcile (selected + excluded == candidates; selection <= n_clips;
    shortfall explanation present when shortfall > 0).
  Any mismatch fails closed. A run without shortlist.json verifies as before.

## Determinism

  shortlist.json contains no wall-clock data. Same inputs -> identical bytes,
  proven by test and by a live re-run comparison.
