from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
import yaml


DEFAULT_FILE = Path.cwd() / ".deploylane" / "vars.yml"


@dataclass(frozen=True)
class VarSpec:
    value: str
    masked: bool
    protected: bool
    environment_scope: str

def _norm_var(v: Dict[str, Any]) -> VarSpec:
    # Normalize missing keys deterministically
    return VarSpec(
        value=str(v.get("value", "")),
        masked=bool(v.get("masked", False)),
        protected=bool(v.get("protected", False)),
        environment_scope=str(v.get("environment_scope", "*") or "*"),
    )

def _safe_value(spec: VarSpec, show_values: bool) -> str:
    # Do not print secret values by default.
    if show_values:
        return spec.value
    if spec.masked:
        return "<masked>"
    # Also hide common secret-like variables deterministically
    upper = spec.value.upper()
    # (We don't inspect value; we inspect key elsewhere. Keep it simple.)
    return "<hidden>"


def write_vars_file(path: Path, data: Dict[str, Any]) -> None:
    """Write variables YAML deterministically."""
    
    path.parent.mkdir(parents=True, exist_ok=True)
    
    path.write_text(
        yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def read_vars_file(path: Path) -> Dict[str, Any]:
    """Read variables YAML."""
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


def demo_template(project: str) -> Dict[str, Any]:
    """Return a demo YAML template if no variables exist."""
    return {
        "project": project,
        "scope": "*",
        "variables": {
            "EXAMPLE_URL": {
                "value": "https://example.com",
                "masked": False,
                "protected": False,
            },
            "SECRET_TOKEN": {
                "value": "change-me",
                "masked": True,
                "protected": True,
            },
        },
    }
    
def load_vars_yml(path: Path) -> Dict[str, Any]:
    """
    Load vars.yml file into a Python dict.
    Returns {} if file is empty or missing.
    """
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        return {}

    return data


def save_vars_yml(path: Path, data: Dict[str, Any]) -> None:
    """
    Save vars.yml back to disk.
    Ensures parent directory exists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            sort_keys=False,
            default_flow_style=False,
        )