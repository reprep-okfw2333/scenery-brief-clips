from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter
from pathlib import Path

from PIL import Image

from scenery_brief_clips.analysis_cache import sha256_file
from scenery_brief_clips.run_lock import exclusive_run_lock

SHORTLIST_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
DEDUP_HAMMING_MAX = 10
DEFAULT_FRAMES_PER_MOMENT = 6
MIN_FRAMES_PER_MOMENT = 2

VALID_MATCH = {"keep", "reject", "uncertain"}
VALID_GEO = {"supported", "uncertain", "conflicting"}

REASON_NO_LABEL = "no label"
REASON_MATCH_REJECT = "visual match rejected"
REASON_MATCH_UNCERTAIN = "visual match uncertain"
REASON_GEO_CONFLICTING = "geographic evidence conflicting"
REASON_BEYOND = "beyond n_clips"
REASON_CONTINUITY_SUSPECT_UNCLEARED = "continuity_suspect_uncleared"

CONTINUITY_OK_NOTE_PREFIX = "continuity_ok:"


class ShortlistError(RuntimeError):
    """Base class for shortlist failures."""


class ShortlistInputError(ShortlistError):
    """Malformed user input (labels file). Exits 2."""


class ShortlistStaleError(ShortlistError):
    """Run or review material must be regenerated before use. Exits 1."""


def image_dhash(image: Image.Image) -> int:
    """64-bit difference hash of a PIL image (structure, not colour)."""
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def frame_dhash(path: str | Path) -> int:
    """64-bit difference hash of one frame (structure, not colour)."""
    with Image.open(path) as image:
        return image_dhash(image)


def signature_for_frames(paths: list[str | Path]) -> list[str]:
    if not paths:
        raise ValueError("signature requires at least one frame")
    return [f"{frame_dhash(path):016x}" for path in paths]


def hamming64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def moments_are_duplicates(signature_a: list, signature_b: list) -> bool:
    """Signature values may be 64-bit ints or hex strings."""
    if not signature_a or not signature_b:
        return False
    hashes_a = [value if isinstance(value, int) else int(value, 16) for value in signature_a]
    hashes_b = [value if isinstance(value, int) else int(value, 16) for value in signature_b]
    return min(hamming64(a, b) for a in hashes_a for b in hashes_b) <= DEDUP_HAMMING_MAX


def _validated_n_clips(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShortlistInputError("n_clips must be an integer")
    if value < 1:
        raise ShortlistInputError("n_clips must be at least 1")
    return value


RESERVED_LABEL_KEYS = frozenset({"excerpts_sha256"})


def video_label_entries(labels: dict) -> dict:
    """Labels keyed by video_id, with reserved metadata keys removed."""
    return {key: value for key, value in labels.items() if key not in RESERVED_LABEL_KEYS}


def validate_label_payload(payload) -> dict:
    if not isinstance(payload, dict):
        raise ShortlistInputError("shortlist labels must be a JSON object keyed by video_id")
    binding = payload.get("excerpts_sha256")
    if binding is not None and (
        not isinstance(binding, str)
        or len(binding) != 64
        or any(ch not in "0123456789abcdef" for ch in binding)
    ):
        raise ShortlistInputError("labels excerpts_sha256 must be a 64-character lowercase hex string")
    for video_id, entries in video_label_entries(payload).items():
        if not isinstance(entries, list):
            raise ShortlistInputError(f"shortlist labels for {video_id} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ShortlistInputError(f"shortlist label entries for {video_id} must be objects")
            index = entry.get("excerpt_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ShortlistInputError(
                    f"shortlist label excerpt_index for {video_id} must be a non-negative integer"
                )
            match = entry.get("match")
            if match not in VALID_MATCH:
                raise ShortlistInputError(
                    f"unknown shortlist match {match!r} for {video_id}; expected {sorted(VALID_MATCH)}"
                )
            geo = entry.get("geo")
            if geo not in VALID_GEO:
                raise ShortlistInputError(
                    f"unknown shortlist geo {geo!r} for {video_id}; expected {sorted(VALID_GEO)}"
                )
            scene_type = entry.get("scene_type")
            if not isinstance(scene_type, str) or not scene_type.strip():
                raise ShortlistInputError(f"shortlist scene_type for {video_id} must be non-empty text")
            note = entry.get("note")
            if note is not None and not isinstance(note, str):
                raise ShortlistInputError(f"shortlist note for {video_id} must be text")
            if "continuity_ok" in entry and not isinstance(entry.get("continuity_ok"), bool):
                raise ShortlistInputError(
                    f"shortlist continuity_ok for {video_id} must be a boolean when present"
                )
    return payload


def _doc_moment(video_id: str, excerpt_index: int, excerpt: dict) -> dict:
    return {
        "video_id": video_id,
        "excerpt_index": excerpt_index,
        "start_s": float(excerpt["start_s"]),
        "end_s": float(excerpt["end_s"]),
        "analysis_cache_key": str(excerpt["analysis_cache_key"]),
    }


def _exclusion_reason(entry: dict) -> str | None:
    if entry["match"] == "reject":
        return REASON_MATCH_REJECT
    if entry["match"] == "uncertain":
        return REASON_MATCH_UNCERTAIN
    if entry["geo"] == "conflicting":
        return REASON_GEO_CONFLICTING
    return None


def continuity_cleared(entry: dict) -> bool:
    """True when a label explicitly clears a continuity_suspect flag."""
    if entry.get("continuity_ok") is True:
        return True
    note = entry.get("note")
    if isinstance(note, str) and note.lstrip().lower().startswith(CONTINUITY_OK_NOTE_PREFIX):
        return True
    return False


def _upstream_continuity_stats(rows: list) -> tuple[int, dict[str, int]]:
    """Count Part 3 continuity rejects that never became shortlist candidates."""
    by_reason: Counter[str] = Counter()
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        rejected = row.get("continuity_rejected") or []
        if not isinstance(rejected, list):
            continue
        for record in rejected:
            total += 1
            if isinstance(record, dict):
                reason = str(record.get("reason") or "continuity reject").strip() or "continuity reject"
            else:
                reason = "continuity reject"
            by_reason[reason] += 1
    return total, dict(sorted(by_reason.items()))


def _shortfall_explanation(
    requested: int,
    selected: int,
    reasons: Counter,
    *,
    upstream_continuity_n: int = 0,
    upstream_continuity_by_reason: dict[str, int] | None = None,
) -> str:
    shortfall = max(0, requested - selected)
    fulfilled = selected >= requested
    if not reasons:
        detail = "none"
    else:
        detail = ", ".join(f"{reason} x{count}" for reason, count in sorted(reasons.items()))
    text = (
        f"requested {requested}, selected {selected}, shortfall {shortfall}, "
        f"request_fulfilled {str(fulfilled).lower()}; "
        f"exclusions: {detail}"
    )
    if upstream_continuity_n:
        upstream = upstream_continuity_by_reason or {}
        if upstream:
            upstream_detail = ", ".join(
                f"{reason} x{count}" for reason, count in sorted(upstream.items())
            )
        else:
            upstream_detail = "unspecified"
        text += (
            f"; upstream continuity rejects (before shortlist candidates): "
            f"{upstream_continuity_n} ({upstream_detail})"
        )
    return text


def parse_frame_hashes(moment: dict) -> tuple[int, ...]:
    """Frame signatures must be 16-character hex strings (or plain ints)."""
    values = moment.get("frame_hashes")
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ShortlistStaleError("review.json frame_hashes must be a list")
    if not values:
        raise ShortlistStaleError("review.json frame_hashes are empty; re-run shortlist-review")
    parsed: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ShortlistStaleError("review.json frame_hashes contains an invalid value")
        if isinstance(value, int):
            parsed.append(value)
            continue
        if (
            isinstance(value, str)
            and len(value) == 16
            and all(character in "0123456789abcdefABCDEF" for character in value)
        ):
            parsed.append(int(value, 16))
            continue
        raise ShortlistStaleError("review.json frame_hashes contains an invalid value")
    return tuple(parsed)


def build_shortlist(
    rows: list[dict],
    review: dict,
    labels: dict,
    n_clips: int,
    bindings: dict,
) -> dict:
    n_clips = _validated_n_clips(n_clips)

    if not isinstance(review, dict) or not isinstance(review.get("moments"), list):
        raise ShortlistStaleError("review.json is invalid; re-run shortlist-review")

    rows_by_video: dict[str, dict] = {}
    row_moments: dict[tuple[str, int], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("video_id") or "")
        rows_by_video[video_id] = row
        for index, excerpt in enumerate(row.get("excerpts") or []):
            row_moments[(video_id, index)] = excerpt

    for video_id in video_label_entries(labels):
        if video_id not in rows_by_video:
            raise ShortlistInputError(f"shortlist labels reference unknown video {video_id}")

    labels_by_key: dict[tuple[str, int], dict] = {}
    for video_id, entries in video_label_entries(labels).items():
        count = len(rows_by_video[video_id].get("excerpts") or [])
        for entry in entries:
            index = int(entry["excerpt_index"])
            if index >= count:
                raise ShortlistInputError(
                    f"shortlist label excerpt_index {index} out of range for {video_id}"
                )
            key = (video_id, index)
            if key in labels_by_key:
                raise ShortlistInputError(f"duplicate shortlist label for {video_id}:{index}")
            labels_by_key[key] = entry

    review_by_key: dict[tuple[str, int], dict] = {}
    for moment in review["moments"]:
        if not isinstance(moment, dict):
            raise ShortlistStaleError("review.json contains a non-object moment")
        try:
            key = (str(moment["video_id"]), int(moment["excerpt_index"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ShortlistStaleError("review.json contains an invalid moment reference") from exc
        if key in review_by_key:
            raise ShortlistStaleError(f"review.json lists {key[0]}:{key[1]} twice")
        review_by_key[key] = moment

    if set(review_by_key) != set(row_moments):
        raise ShortlistStaleError(
            "review.json does not cover the analyzed moments exactly; re-run shortlist-review"
        )

    ordered_keys = sorted(row_moments)
    for key, moment in review_by_key.items():
        excerpt = row_moments[key]
        if (
            float(moment.get("start_s", -1.0)) != float(excerpt["start_s"])
            or float(moment.get("end_s", -1.0)) != float(excerpt["end_s"])
            or str(moment.get("analysis_cache_key") or "") != str(excerpt["analysis_cache_key"])
        ):
            raise ShortlistStaleError(
                f"review moment {key[0]}:{key[1]} does not match excerpts.json; re-run shortlist-review"
            )

    excluded: dict[tuple[str, int], list[str]] = {}
    kept: list[tuple[tuple[str, int], list[str]]] = []
    for key in ordered_keys:
        entry = labels_by_key.get(key)
        if entry is None:
            excluded[key] = [REASON_NO_LABEL]
            continue
        reason = _exclusion_reason(entry)
        if reason is not None:
            excluded[key] = [reason]
            continue
        # continuity_suspect requires an explicit clearance before keep can stick.
        if review_by_key[key].get("continuity_suspect") and not continuity_cleared(entry):
            excluded[key] = [REASON_CONTINUITY_SUSPECT_UNCLEARED]
            continue
        signature = parse_frame_hashes(review_by_key[key])
        duplicate_of = next(
            (kept_key for kept_key, kept_signature in kept if moments_are_duplicates(signature, kept_signature)),
            None,
        )
        if duplicate_of is not None:
            excluded[key] = [f"duplicate of {duplicate_of[0]}:{duplicate_of[1]}"]
            continue
        kept.append((key, signature))

    buckets: dict[str, list[tuple[str, int]]] = {}
    for key, _signature in kept:
        scene_type = str(labels_by_key[key]["scene_type"]).strip()
        buckets.setdefault(scene_type, []).append(key)
    type_order = list(buckets)
    usage: Counter[str] = Counter()
    picked: list[tuple[str, int]] = []
    cursor = 0
    while len(picked) < n_clips and any(buckets.values()):
        scene_type = type_order[cursor % len(type_order)]
        bucket = buckets[scene_type]
        if not bucket:
            cursor += 1
            continue
        best = min(bucket, key=lambda candidate: (usage[candidate[0]], bucket.index(candidate)))
        bucket.remove(best)
        picked.append(best)
        usage[best[0]] += 1
        cursor += 1
    for key, _signature in kept:
        if key not in picked:
            excluded[key] = [REASON_BEYOND]

    selected_entries = []
    for key in picked:
        entry = labels_by_key[key]
        record = _doc_moment(key[0], key[1], row_moments[key])
        record["scene_type"] = str(entry["scene_type"]).strip()
        record["geo"] = entry["geo"]
        record["flags"] = ["geo_uncertain"] if entry["geo"] == "uncertain" else []
        record["reason"] = "selected"
        selected_entries.append(record)

    excluded_entries = []
    for key in ordered_keys:
        if key not in excluded:
            continue
        record = _doc_moment(key[0], key[1], row_moments[key])
        record["reasons"] = excluded[key]
        excluded_entries.append(record)

    reasons_counter: Counter[str] = Counter()
    for reasons in excluded.values():
        for reason in reasons:
            reasons_counter[reason] += 1

    sources_without_moments = sum(
        1
        for row in rows
        if isinstance(row, dict) and not (row.get("excerpts") or [])
    )

    upstream_n, upstream_by_reason = _upstream_continuity_stats(rows)
    n_selected = len(selected_entries)
    request_fulfilled = n_selected >= n_clips

    return {
        "schema_version": SHORTLIST_SCHEMA_VERSION,
        "n_clips_requested": n_clips,
        "bindings": dict(bindings),
        "selected": selected_entries,
        "excluded": excluded_entries,
        "counts": {
            "n_candidates": len(ordered_keys),
            "n_selected": n_selected,
            "n_excluded": len(excluded_entries),
            "n_excluded_by_reason": dict(sorted(reasons_counter.items())),
            "n_sources_without_moments": sources_without_moments,
            "request_fulfilled": request_fulfilled,
            "n_continuity_rejected_upstream": upstream_n,
            "n_continuity_rejected_upstream_by_reason": upstream_by_reason,
        },
        "shortfall": {
            "count": max(0, n_clips - n_selected),
            "explanation": _shortfall_explanation(
                n_clips,
                n_selected,
                reasons_counter,
                upstream_continuity_n=upstream_n,
                upstream_continuity_by_reason=upstream_by_reason,
            ),
        },
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    token = uuid.uuid4().hex
    tmp = path.with_name(f"{path.name}.{token}.tmp")
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_run_bytes(path: Path, name: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ShortlistStaleError(f"{name} is unreadable: {exc}") from exc


def parse_run_json(data: bytes, name: str):
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ShortlistStaleError(f"{name} is not valid JSON: {exc}") from exc


def shortlist_apply_run(run_dir: str | Path, labels_path: str | Path) -> dict:
    run_dir = Path(run_dir)
    with exclusive_run_lock(run_dir):
        return _shortlist_apply_locked(run_dir, labels_path)


def _shortlist_apply_locked(run_dir: Path, labels_path: str | Path) -> dict:
    paths = {
        "constraint.json": run_dir / "constraint.json",
        "ranked.json": run_dir / "ranked.json",
        "excerpts.json": run_dir / "excerpts.json",
        "analysis_manifest.json": run_dir / "analysis_manifest.json",
        "review.json": run_dir / "review.json",
    }
    for name, path in paths.items():
        if not path.is_file():
            raise ShortlistStaleError(f"missing {name}; run the earlier stages first")

    manifest = parse_run_json(
        read_run_bytes(paths["analysis_manifest.json"], "analysis_manifest.json"),
        "analysis_manifest.json",
    )
    if not isinstance(manifest, dict):
        raise ShortlistStaleError("analysis_manifest.json is not a JSON object; the run data is broken")

    input_bytes: dict[str, bytes] = {}
    for name in ("ranked.json", "constraint.json", "excerpts.json"):
        data = read_run_bytes(paths[name], name)
        input_bytes[name] = data
        expected = manifest.get(
            {
                "ranked.json": "ranked_sha256",
                "constraint.json": "constraint_sha256",
                "excerpts.json": "excerpts_sha256",
            }[name]
        )
        if hashlib.sha256(data).hexdigest() != expected:
            raise ShortlistStaleError(f"{name} changed after analysis; re-run analyze")

    constraint = parse_run_json(input_bytes["constraint.json"], "constraint.json")
    if not isinstance(constraint, dict):
        raise ShortlistStaleError("constraint.json is not a JSON object; the run data is broken")
    try:
        n_clips = _validated_n_clips(constraint.get("n_clips"))
    except ShortlistInputError as exc:
        raise ShortlistInputError(f"constraint.json has no valid n_clips: {exc}") from exc

    ranked = parse_run_json(input_bytes["ranked.json"], "ranked.json")
    if not isinstance(ranked, list):
        raise ShortlistStaleError("ranked.json is not a list of rows; the run data is broken")

    labels_path = Path(labels_path)
    if not labels_path.is_file():
        raise ShortlistInputError(f"missing labels file {labels_path}")
    labels_path = labels_path.resolve()
    run_root = run_dir.resolve()
    if not labels_path.is_relative_to(run_root):
        raise ShortlistInputError(f"labels file must stay inside the run dir: {labels_path}")
    try:
        raw_labels = json.loads(labels_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShortlistInputError(f"cannot read labels file: {exc}") from exc
    labels = validate_label_payload(raw_labels)

    review_bytes = read_run_bytes(paths["review.json"], "review.json")
    review = parse_run_json(review_bytes, "review.json")
    if not isinstance(review, dict):
        raise ShortlistStaleError("review.json is not a JSON object; re-run shortlist-review")
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ShortlistStaleError("review.json has an unsupported schema; re-run shortlist-review")
    if review.get("excerpts_sha256") != manifest.get("excerpts_sha256"):
        raise ShortlistStaleError("review material is stale; re-run shortlist-review")

    rows = parse_run_json(input_bytes["excerpts.json"], "excerpts.json")
    if not isinstance(rows, list):
        raise ShortlistStaleError("excerpts.json is not a list of rows; the run data is broken")
    bindings = {
        "excerpts_sha256": hashlib.sha256(input_bytes["excerpts.json"]).hexdigest(),
        "generation_id": manifest.get("generation_id"),
        "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "labels_sha256": sha256_file(labels_path),
        "labels_path": labels_path.relative_to(run_root).as_posix(),
    }
    labels_binding = labels.get("excerpts_sha256") if isinstance(labels, dict) else None
    if labels_binding is not None and labels_binding != bindings["excerpts_sha256"]:
        raise ShortlistInputError(
            "labels were written for a different analysis generation; re-run shortlist-review and re-label"
        )
    doc = build_shortlist(rows, review, labels, n_clips, bindings)
    _write_json_atomic(run_dir / "shortlist.json", doc)
    return doc
