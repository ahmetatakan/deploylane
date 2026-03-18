from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

LOG_FILE = ".deploy-history.jsonl"


def _log_path(ws_path: Path) -> Path:
    return ws_path.parent / LOG_FILE


def append_log(ws_path: Path, entry: Dict) -> None:
    """Append a single deployment record to the workspace log file."""
    path = _log_path(ws_path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(ws_path: Path, project: Optional[str] = None, limit: int = 20) -> List[Dict]:
    """Return up to `limit` recent log entries, optionally filtered by project."""
    path = _log_path(ws_path)
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if project and entry.get("project") != project:
                    continue
                entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries[-limit:]


def make_entry(
    project: str,
    gitlab_project: str,
    target: str,
    image_tag: str,
    strategy: str,
    host: str,
    deploy_dir: str,
    status: str,
    error: str = "",
) -> Dict:
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project":        project,
        "gitlab_project": gitlab_project,
        "target":         target,
        "image_tag":      image_tag,
        "strategy":       strategy,
        "host":           host,
        "deploy_dir":     deploy_dir,
        "status":         status,   # "ok" | "failed" | "dry-run"
        "error":          error,
    }
