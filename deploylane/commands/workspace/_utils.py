from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...workspace import (
    find_workspace_file,
    load_workspace,
    project_vars_yml,
    WORKSPACE_FILE_NAME,
)
from .._utils import _err


def _ws_file_option() -> Path:
    """Return workspace.yml path: auto-discover or default to cwd."""
    discovered = find_workspace_file()
    return discovered if discovered else Path(WORKSPACE_FILE_NAME)


def _resolve_ws(file: Optional[Path]) -> Path:
    ws_path = file or _ws_file_option()
    if not ws_path.exists():
        _err("workspace.yml not found. Run: dlane init")
    return ws_path


def _ws_vars_file(name: str, ws_path: Path) -> Path:
    """Resolve .deploylane/vars.yml for a workspace project."""
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")
    return project_vars_yml(project, ws_path.parent)
