from __future__ import annotations

import hashlib
import urllib.request
from collections.abc import Callable
from pathlib import Path

DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) scenery-brief-clips/0.1"
MAX_SHEET_BYTES = 5_000_000


def http_get(url: str, timeout: int = 30, max_bytes: int = MAX_SHEET_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("storyboard sheet exceeds size cap")
    return data


def cached_fetcher(cache_dir: str | Path, getter: Callable[[str], bytes] | None = None) -> Callable[[str], bytes]:
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    get = getter or http_get

    def fetch(url: str) -> bytes:
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".jpg"
        path = root / name
        if path.is_file() and path.stat().st_size > 0:
            return path.read_bytes()
        data = get(url)
        path.write_bytes(data)
        return data

    return fetch
