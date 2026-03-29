from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


_STATE_DIR = ".state"


def state_path(base: Path, target: str) -> Path:
    return base / _STATE_DIR / f"{target}.json"


def file_hash(path: Path) -> Optional[str]:
    """Short SHA-256 of file content. None if file missing."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def read_state(base: Path, target: str) -> Optional[dict]:
    """Read pull/push state for a target. Returns None if not found."""
    p = state_path(base, target)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_state(
    base: Path,
    target: str,
    host: str,
    file_hashes: Dict[str, Optional[str]],
) -> None:
    """Write state after a pull or push."""
    state_dir = base / _STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(base)

    data = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "host": host,
        "files": file_hashes,
    }
    state_path(base, target).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def local_file_hashes(
    base: Path,
    strategy: str,
    compose_file: str,
    deploy_script: str,
) -> Dict[str, Optional[str]]:
    """Compute hashes of local deploy files, keyed by server-side filename."""
    compose_src = base / "compose" / f"{strategy}.yml"
    if not compose_src.exists():
        compose_src = base / "compose" / "plain.yml"

    return {
        compose_file: file_hash(compose_src),
        deploy_script: file_hash(base / "scripts" / "deploy.sh"),
    }


def changed_since_state(
    state: dict,
    current_hashes: Dict[str, Optional[str]],
) -> List[str]:
    """Return filenames that changed locally since last pull/push."""
    state_files = state.get("files", {})
    return [
        fname
        for fname, current_hash in current_hashes.items()
        if current_hash != state_files.get(fname)
    ]


def _ensure_gitignore(base: Path) -> None:
    """Add .state/ to .deploylane/.gitignore if not already there."""
    gi = base / ".gitignore"
    entry = ".state/\n"
    if gi.exists():
        content = gi.read_text(encoding="utf-8")
        if ".state/" not in content:
            gi.write_text(content.rstrip() + "\n" + entry, encoding="utf-8")
    else:
        gi.write_text(entry, encoding="utf-8")
