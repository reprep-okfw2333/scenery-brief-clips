"""Keep pytest off the small host /tmp.

Export refuses when the filesystem holding the project root has under 2GB free.
The default pytest temp directory is host /tmp, a small tmpfs on this host, so
that guard fired on every export test even though the project disk was fine.
Project tmp/ is on the large disk and is the only temp location this repo allows.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_TMP = Path(__file__).resolve().parents[1] / "tmp"


def pytest_configure(config) -> None:
    _PROJECT_TMP.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(_PROJECT_TMP)
    if not getattr(config.option, "basetemp", None):
        base = _PROJECT_TMP / "pytest"
        base.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(base)
