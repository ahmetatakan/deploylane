from __future__ import annotations

import os
import stat
from pathlib import Path


def ensure_dir(path: Path) -> None:
    """Create directory tree if missing."""
    path.mkdir(parents=True, exist_ok=True)


def chmod_600(path: Path) -> None:
    """Best-effort: restrict file permissions to owner read/write (0600)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except Exception:
        # On some FS/OS combinations chmod might fail; ignore silently.
        pass