from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_run_lock(run_dir: str | Path) -> Iterator[None]:
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    lock_path = run_path / ".pipeline.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
