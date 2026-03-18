from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


WORKSPACE_DIR_NAME  = ".workspace"
WORKSPACE_FILE_NAME = ".workspace/workspace.yml"


@dataclass
class WorkspaceProject:
    name: str               # short alias, e.g. "backend"
    path: str               # relative to workspace.yml directory, e.g. "../backend"
    gitlab_project: str     # GitLab path_with_namespace, e.g. "acme/backend"
    strategy: str = "plain"  # deployment strategy: plain | bluegreen
    description: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class WorkspaceFile:
    version: int
    projects: List[WorkspaceProject]
    default_profile: str = "default"


# ─── I/O ─────────────────────────────────────────────────────────────────────

def load_workspace(path: Path) -> WorkspaceFile:
    """Parse workspace.yml. Raises FileNotFoundError or ValueError."""
    if not path.exists():
        raise FileNotFoundError(f"workspace.yml not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("workspace.yml: root must be a mapping.")

    version = raw.get("version", 1)
    default_profile = raw.get("default_profile", "default") or "default"

    projects: List[WorkspaceProject] = []
    for item in raw.get("projects", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        path_val = str(item.get("path") or "").strip()
        gitlab_project = str(item.get("gitlab_project") or "").strip()
        strategy = str(item.get("strategy") or item.get("stack") or "plain").strip().lower()
        if not name or not path_val:
            continue
        projects.append(WorkspaceProject(
            name=name,
            path=path_val,
            gitlab_project=gitlab_project,
            strategy=strategy,
            description=str(item.get("description") or "").strip(),
            tags=[str(t) for t in (item.get("tags") or []) if t],
        ))

    return WorkspaceFile(version=int(version), projects=projects, default_profile=str(default_profile))


def save_workspace(ws: WorkspaceFile, path: Path) -> None:
    """Write workspace.yml deterministically (projects sorted by name)."""
    sorted_projects = sorted(ws.projects, key=lambda p: p.name.lower())

    projects_data = []
    for p in sorted_projects:
        entry: Dict[str, Any] = {
            "name": p.name,
            "path": p.path,
            "gitlab_project": p.gitlab_project,
            "strategy": p.strategy,
        }
        if p.description:
            entry["description"] = p.description
        if p.tags:
            entry["tags"] = sorted(p.tags)
        projects_data.append(entry)

    data: Dict[str, Any] = {
        "version": ws.version,
        "default_profile": ws.default_profile,
        "projects": projects_data,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def find_workspace_file(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from start (or cwd) looking for workspace.yml, like git does for .git."""
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / WORKSPACE_FILE_NAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


# ─── Status helpers ───────────────────────────────────────────────────────────

def project_root(project: WorkspaceProject, workspace_dir: Path) -> Path:
    """Resolve absolute path to a project directory."""
    return (workspace_dir / project.path).resolve()


def project_deploy_yml(project: WorkspaceProject, workspace_dir: Path) -> Path:
    return project_root(project, workspace_dir) / ".deploylane" / "deploy.yml"


def project_vars_yml(project: WorkspaceProject, workspace_dir: Path) -> Path:
    return project_root(project, workspace_dir) / ".deploylane" / "vars.yml"


def project_rendered_dir(project: WorkspaceProject, workspace_dir: Path) -> Path:
    return project_root(project, workspace_dir) / ".deploylane" / "rendered"


def get_project_status(project: WorkspaceProject, workspace_dir: Path) -> Dict[str, Any]:
    """
    Returns a status dict for one project:
      - root_exists: bool
      - deploy_yml_exists: bool
      - vars_yml_exists: bool
      - strategy: str (from deploy.yml if readable, else from workspace entry)
      - targets: list[str]
      - default_target: str
      - rendered_targets: list[str]
    """
    root = project_root(project, workspace_dir)
    deploy_yml = project_deploy_yml(project, workspace_dir)
    vars_yml = project_vars_yml(project, workspace_dir)
    rendered_dir = project_rendered_dir(project, workspace_dir)

    status: Dict[str, Any] = {
        "root_exists": root.exists(),
        "deploy_yml_exists": deploy_yml.exists(),
        "vars_yml_exists": vars_yml.exists(),
        "strategy": project.strategy,
        "targets": [],
        "default_target": "",
        "rendered_targets": [],
    }

    if deploy_yml.exists():
        try:
            raw = yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                status["strategy"] = str(raw.get("strategy") or raw.get("stack") or project.strategy).strip()
                targets_raw = raw.get("targets") or {}
                if isinstance(targets_raw, dict):
                    status["targets"] = sorted(targets_raw.keys())
                status["default_target"] = str(raw.get("default_target") or "").strip()
        except Exception:
            pass

    if rendered_dir.exists():
        status["rendered_targets"] = sorted(
            d.name for d in rendered_dir.iterdir()
            if d.is_dir() and (d / ".env").exists()
        )

    return status
