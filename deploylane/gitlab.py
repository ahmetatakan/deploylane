from __future__ import annotations

from dataclasses import dataclass
import requests
from typing import Optional, List, Dict, Any
from urllib.parse import quote

@dataclass
class GitLabUser:
    id: int
    username: str
    name: str

@dataclass
class Project:
    id: int
    path_with_namespace: str
    web_url: Optional[str] = None
    default_branch: Optional[str] = None

@dataclass
class ProjectVariable:
    key: str
    value: Optional[str]  # GitLab masked ise value gelmeyebilir / boş olabilir
    protected: bool
    masked: bool
    environment_scope: str

class GitLabError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    """GitLab PAT auth header."""
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


def whoami(host: str, token: str, timeout_s: int = 15) -> GitLabUser:
    """Validate token by calling GET /api/v4/user."""
    url = f"{host}/api/v4/user"
    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout_s)
    except requests.RequestException as e:
        raise GitLabError(f"GitLab unreachable: {e}") from e

    if r.status_code == 401:
        raise GitLabError("Unauthorized (token invalid or expired).")
    if r.status_code >= 400:
        raise GitLabError(f"GitLab error {r.status_code}: {r.text[:300]}")

    data = r.json()
    return GitLabUser(
        id=int(data.get("id", 0)),
        username=str(data.get("username", "")),
        name=str(data.get("name", "")),
    )
    
def list_projects(
    host: str,
    token: str,
    search: Optional[str] = None,
    owned: bool = False,
    membership: bool = True,
    timeout_s: int = 15,
) -> List[Project]:
    """
    List GitLab projects visible to the current user.
    Uses pagination (per_page=100).
    """

    params: Dict[str, Any] = {"per_page": 100,"simple": "false",}
    if search:
        params["search"] = search
    if owned:
        params["owned"] = "true"
    if membership:
        params["membership"] = "true"

    out: List[Project] = []
    page = 1

    while True:
        params["page"] = page

        url = f"{host}/api/v4/projects"

        try:
            r = requests.get(url, headers=_headers(token), params=params, timeout=timeout_s)
        except requests.RequestException as e:
            raise GitLabError(f"GitLab unreachable: {e}") from e

        if r.status_code == 401:
            raise GitLabError("Unauthorized (token invalid or expired).")
        if r.status_code >= 400:
            raise GitLabError(f"GitLab error {r.status_code}: {r.text[:300]}")

        items = r.json()
        if not isinstance(items, list) or not items:
            break

        for it in items:
            out.append(
                Project(
                    id=int(it["id"]),
                    path_with_namespace=str(it["path_with_namespace"]),
                    default_branch=(it.get("default_branch") or None),
                    web_url=(it.get("web_url") or it.get("http_url_to_repo") or None),
                )
            )

        # pagination header
        next_page = r.headers.get("X-Next-Page")
        if not next_page:
            break

        page = int(next_page)

    return out

def _url(host: str, path: str) -> str:
    return f"{host}/api/v4{path}"


def get_project_by_path(host: str, token: str, path_with_namespace: str, timeout_s: int = 20) -> Project:
    """
    Deterministic project resolve:
      GET /projects/:urlencoded(path_with_namespace)
    Example: <group>/<project> -> <group>%2F<project>
    """
    enc = quote(path_with_namespace, safe="")
    url = _url(host, f"/projects/{enc}")

    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout_s)
    except requests.RequestException as e:
        raise GitLabError(f"GitLab unreachable: {e}") from e

    if r.status_code in (401, 403):
        raise GitLabError("Not authorized. Token may be invalid or missing scope.")
    if r.status_code == 404:
        raise GitLabError(f"Project not found: {path_with_namespace}")
    if r.status_code != 200:
        raise GitLabError(f"GitLab API error: {r.status_code} {r.text[:300]}")

    data = r.json()
    return Project(
        id=int(data.get("id", 0)),
        path_with_namespace=str(data.get("path_with_namespace", path_with_namespace)),
        web_url=(str(data["web_url"]) if data.get("web_url") else None),
        default_branch=(str(data["default_branch"]) if data.get("default_branch") else None),
    )


def list_project_variables(host: str, token: str, project_id: int, timeout_s: int = 20) -> List[ProjectVariable]:
    """
    GET /projects/:id/variables
    Note: masked variables may not return the actual value.
    """
    url = _url(host, f"/projects/{project_id}/variables")

    try:
        r = requests.get(url, headers=_headers(token), params={"per_page": 100}, timeout=timeout_s)
    except requests.RequestException as e:
        raise GitLabError(f"GitLab unreachable: {e}") from e

    if r.status_code in (401, 403):
        raise GitLabError("Not authorized. Token may be invalid or missing scope.")
    if r.status_code != 200:
        raise GitLabError(f"GitLab API error: {r.status_code} {r.text[:300]}")

    items = r.json()
    out: List[ProjectVariable] = []

    if not isinstance(items, list):
        return out

    for it in items:
        if not isinstance(it, dict):
            continue
        out.append(
            ProjectVariable(
                key=str(it.get("key", "")),
                value=(str(it["value"]) if it.get("value") is not None else None),
                protected=bool(it.get("protected", False)),
                masked=bool(it.get("masked", False)),
                environment_scope=str(it.get("environment_scope", "*")),
            )
        )

    # Deterministic ordering
    out.sort(key=lambda v: v.key)
    return out

def set_project_variable(
    host: str,
    token: str,
    project_id: int,
    key: str,
    value: str,
    protected: bool = False,
    masked: bool = False,
    environment_scope: str = "*",
    variable_type: str = "env_var",  # "env_var" or "file"
    timeout_s: int = 20,
) -> None:
    """
    Create or update a GitLab project variable.
    GitLab API: POST /projects/:id/variables (create) or PUT /projects/:id/variables/:key (update)
    """
    base_url = f"{host}/api/v4/projects/{project_id}/variables"
    headers = _headers(token)

    payload: Dict[str, Any] = {
        "key": key,
        "value": value,
        "protected": protected,
        "masked": masked,
        "environment_scope": environment_scope,
        "variable_type": variable_type,
    }

    # 1) Try create
    try:
        r = requests.post(base_url, headers=headers, data=payload, timeout=timeout_s)
    except requests.RequestException as e:
        raise GitLabError(f"GitLab unreachable: {e}") from e

    if r.status_code in (200, 201):  # created
        return

    # 2) If already exists -> update
    if r.status_code == 400 and "has already been taken" in (r.text or ""):
        try:
            r2 = requests.put(f"{base_url}/{key}", headers=headers, data=payload, timeout=timeout_s)
        except requests.RequestException as e:
            raise GitLabError(f"GitLab unreachable: {e}") from e

        if r2.status_code in (200, 201):
            return
        if r2.status_code in (401, 403):
            raise GitLabError("Not authorized (token invalid or missing scope).")
        raise GitLabError(f"GitLab API error (update): {r2.status_code} {r2.text[:300]}")

    if r.status_code in (401, 403):
        raise GitLabError("Not authorized (token invalid or missing scope).")

    raise GitLabError(f"GitLab API error (create): {r.status_code} {r.text[:300]}")


def delete_project_variable(
    host: str,
    token: str,
    project_id: int,
    key: str,
    environment_scope: str = "*",
    timeout_s: int = 20,
) -> None:
    """
    Delete a GitLab project variable.
    GitLab API: DELETE /projects/:id/variables/:key?filter[environment_scope]=*
    (GitLab supports filtering by environment scope; some instances accept environment_scope directly.)
    """
    url = f"{host}/api/v4/projects/{project_id}/variables/{key}"
    headers = _headers(token)

    params = {"filter[environment_scope]": environment_scope}

    try:
        r = requests.delete(url, headers=headers, params=params, timeout=timeout_s)
    except requests.RequestException as e:
        raise GitLabError(f"GitLab unreachable: {e}") from e

    if r.status_code in (200, 204):
        return
    if r.status_code == 404:
        raise GitLabError("Variable not found.")
    if r.status_code in (401, 403):
        raise GitLabError("Not authorized (token invalid or missing scope).")

    raise GitLabError(f"GitLab API error: {r.status_code} {r.text[:300]}")