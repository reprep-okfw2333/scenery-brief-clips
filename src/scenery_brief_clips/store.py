from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MetadataCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, video_id: str) -> dict | None:
        path = self._path(video_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, video_id: str, info: dict) -> None:
        path = self._path(video_id)
        path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _path(self, video_id: str) -> Path:
        safe = video_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"


def write_run(
    runs_root: str | Path,
    constraint: Any,
    candidates: list[Any],
    rejected: list[Any],
    log_text: str = "",
    run_id: str | None = None,
) -> Path:
    runs_root = Path(runs_root)
    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "constraint.json", constraint)
    _write_json(run_dir / "candidates.json", candidates)
    _write_json(run_dir / "rejected.json", rejected)
    (run_dir / "log.txt").write_text(log_text, encoding="utf-8")
    return run_dir


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_to_jsonable(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


def write_json_atomic(path: str | Path, value: Any) -> None:
    path = Path(path)
    payload = (json.dumps(_to_jsonable(value), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
