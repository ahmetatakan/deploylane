from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional
import hashlib
import json


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_entry(path: Path, root: Optional[Path] = None) -> Dict[str, Any]:
    p = path.resolve()
    rel = None
    if root is not None:
        try:
            rel = str(p.relative_to(root.resolve()))
        except Exception:
            rel = None

    return {
        "path": rel or str(p),
        "sha256": sha256_file(p),
        "bytes": p.stat().st_size,
    }


def write_json_deterministic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")
    
def sha256_string(s: str) -> str:
    h = hashlib.sha256()
    h.update(s.encode("utf-8"))
    return h.hexdigest()