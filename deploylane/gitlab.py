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
    value: Optional[str]  # masked variables may not return value
    protected: bool
    masked: bool
    environment_scope: str


class GitLabError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    return {"PRIVATE-TOKEN": token, "Accept": "application/json"}


def _url(host: str, path: str) -> str:
    host = (host or "").rstrip("/")
    return f"{host}/api/v4{path}"


def _unreachable_error(host: str, cause: Exception) -> GitLabError:
    return GitLabError(
        f"Cannot reach GitLab at {host}\n"
        f"  Check the host URL and your network connection.\n"
        f"  If the URL is wrong, run: dlane login --host <correct-url>\n"
        f"  Cause: {cause}"
    )


def _auth_error(host: str) -> GitLabError:
    return GitLabError(
        f"Authentication failed for {host}\n"
        f"  Your token may be invalid, expired, or missing the 'api' scope.\n"
        f"  Fix: GitLab → User Settings → Access Tokens → create a token with 'api' scope\n"
        f"  Then run: dlane login --host {host}"
    )


def _scope_error(host: str) -> GitLabError:
    return GitLabError(
        f"Access denied. Your token is missing the required 'api' scope.\n"
        f"  Fix: GitLab → User Settings → Access Tokens → enable 'api' scope\n"
        f"  Then run: dlane login --host {host}"
    )


def whoami(host: str, token: str, timeout_s: int = 15) -> GitLabUser:
    url = _url(host, "/user")
    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e

    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code >= 400:
        raise GitLabError(f"Unexpected response from GitLab (HTTP {r.status_code}).")

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
    params: Dict[str, Any] = {"per_page": 100, "simple": "false"}
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
        url = _url(host, "/projects")

        try:
            r = requests.get(url, headers=_headers(token), params=params, timeout=timeout_s)
        except requests.RequestException as e:
            raise _unreachable_error(host, e) from e

        if r.status_code == 401:
            raise _auth_error(host)
        if r.status_code >= 400:
            raise GitLabError(f"Unexpected response from GitLab (HTTP {r.status_code}).")

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

        next_page = r.headers.get("X-Next-Page")
        if not next_page:
            break
        page = int(next_page)

    return out


def get_project_by_path(host: str, token: str, path_with_namespace: str, timeout_s: int = 20) -> Project:
    enc = quote(path_with_namespace, safe="")
    url = _url(host, f"/projects/{enc}")

    try:
        r = requests.get(url, headers=_headers(token), timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e

    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code == 403:
        raise _scope_error(host)
    if r.status_code == 404:
        raise GitLabError(
            f"Project not found: '{path_with_namespace}'\n"
            f"  Check the project path in your deploy.yml or workspace.yml.\n"
            f"  Browse available projects: dlane gitlab list --search <name>"
        )
    if r.status_code != 200:
        raise GitLabError(f"Unexpected response from GitLab (HTTP {r.status_code}).")

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
    Handles pagination (per_page=100).
    """
    url = _url(host, f"/projects/{project_id}/variables")

    out: List[ProjectVariable] = []
    page = 1
    params: Dict[str, Any] = {"per_page": 100}

    while True:
        params["page"] = page
        try:
            r = requests.get(url, headers=_headers(token), params=params, timeout=timeout_s)
        except requests.RequestException as e:
            raise _unreachable_error(host, e) from e

        if r.status_code == 401:
            raise _auth_error(host)
        if r.status_code == 403:
            raise _scope_error(host)
        if r.status_code != 200:
            raise GitLabError(f"Unexpected response from GitLab (HTTP {r.status_code}).")

        items = r.json()
        if not isinstance(items, list) or not items:
            break

        for it in items:
            if not isinstance(it, dict):
                continue
            out.append(
                ProjectVariable(
                    key=str(it.get("key", "")).strip(),
                    value=(str(it["value"]) if it.get("value") is not None else None),
                    protected=bool(it.get("protected", False)),
                    masked=bool(it.get("masked", False)),
                    environment_scope=str(it.get("environment_scope", "*") or "*").strip() or "*",
                )
            )

        next_page = r.headers.get("X-Next-Page")
        if not next_page:
            break
        page = int(next_page)

    # Deterministic ordering by (key, env_scope)
    out.sort(key=lambda v: (v.key.lower(), v.environment_scope))
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
    variable_type: str = "env_var",
    timeout_s: int = 20,
) -> None:
    base_url = _url(host, f"/projects/{project_id}/variables")
    headers = _headers(token)

    key_norm = str(key).strip()
    if not key_norm:
        raise GitLabError("Variable key is empty.")
    key_enc = quote(key_norm, safe="")

    env_scope = str(environment_scope or "*").strip() or "*"

    payload_create: Dict[str, Any] = {
        "key": key_norm,
        "value": value,
        "protected": protected,
        "masked": masked,
        "environment_scope": env_scope,
        "variable_type": variable_type,
    }

    payload_update: Dict[str, Any] = {
        "value": value,
        "protected": protected,
        "masked": masked,
        "environment_scope": env_scope,
        "variable_type": variable_type,
    }

    def _do_update() -> None:
        put_url = f"{base_url}/{key_enc}"
        params = {"filter[environment_scope]": env_scope}
        try:
            r2 = requests.put(put_url, headers=headers, params=params, data=payload_update, timeout=timeout_s)
        except requests.RequestException as e:
            raise _unreachable_error(host, e) from e

        if r2.status_code in (200, 201):
            return
        if r2.status_code == 401:
            raise _auth_error(host)
        if r2.status_code == 403:
            raise _scope_error(host)
        if r2.status_code == 409:
            raise GitLabError(
                f"Conflict updating variable '{key_norm}' (env={env_scope}).\n"
                f"  Multiple variables with the same key exist in GitLab.\n"
                f"  Fix: check 'environment_scope' values in your vars.yml."
            )
        raise GitLabError(f"Failed to update variable '{key_norm}' (HTTP {r2.status_code}).")

    # 1) Try create
    try:
        r = requests.post(base_url, headers=headers, data=payload_create, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e

    if r.status_code in (200, 201):
        return

    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code == 403:
        raise _scope_error(host)

    # 2) Create failed - try update for common "exists" cases
    if r.status_code in (400, 409):
        _do_update()
        return

    raise GitLabError(f"Failed to create variable '{key_norm}' (HTTP {r.status_code}).")


def get_repository_file(
    host: str,
    token: str,
    project_id: int,
    file_path: str,
    ref: str = "main",
    timeout_s: int = 20,
) -> str:
    """GET /projects/:id/repository/files/:file_path — returns decoded content."""
    import base64
    enc = quote(file_path, safe="")
    url = _url(host, f"/projects/{project_id}/repository/files/{enc}")
    try:
        r = requests.get(url, headers=_headers(token), params={"ref": ref}, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e
    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code == 404:
        raise GitLabError(f"File not found: {file_path} (ref={ref})")
    if r.status_code != 200:
        raise GitLabError(f"Unexpected response (HTTP {r.status_code}).")
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8")


def create_branch(
    host: str,
    token: str,
    project_id: int,
    branch: str,
    ref: str = "main",
    timeout_s: int = 20,
) -> None:
    """POST /projects/:id/repository/branches"""
    url = _url(host, f"/projects/{project_id}/repository/branches")
    try:
        r = requests.post(url, headers=_headers(token), json={"branch": branch, "ref": ref}, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e
    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code in (200, 201):
        return
    if r.status_code == 400 and "already exists" in (r.text or "").lower():
        return  # branch already exists, fine
    raise GitLabError(f"Failed to create branch '{branch}' (HTTP {r.status_code}).")


def upsert_repository_file(
    host: str,
    token: str,
    project_id: int,
    file_path: str,
    content: str,
    branch: str,
    commit_message: str,
    timeout_s: int = 20,
) -> None:
    """Create or update a file in the repository."""
    import base64
    enc = quote(file_path, safe="")
    url = _url(host, f"/projects/{project_id}/repository/files/{enc}")
    payload = {
        "branch": branch,
        "content": content,
        "commit_message": commit_message,
        "encoding": "text",
    }
    try:
        # Try update first
        r = requests.put(url, headers=_headers(token), json=payload, timeout=timeout_s)
        if r.status_code in (200, 201):
            return
        if r.status_code == 404:
            # File doesn't exist, create it
            r = requests.post(url, headers=_headers(token), json=payload, timeout=timeout_s)
            if r.status_code in (200, 201):
                return
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e
    if r.status_code == 401:
        raise _auth_error(host)
    raise GitLabError(f"Failed to upsert file '{file_path}' (HTTP {r.status_code}): {r.text}")


def create_merge_request(
    host: str,
    token: str,
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str = "",
    timeout_s: int = 20,
) -> str:
    """POST /projects/:id/merge_requests — returns MR web URL."""
    url = _url(host, f"/projects/{project_id}/merge_requests")
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": True,
    }
    try:
        r = requests.post(url, headers=_headers(token), json=payload, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e
    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code in (200, 201):
        return str(r.json().get("web_url", ""))
    raise GitLabError(f"Failed to create MR (HTTP {r.status_code}): {r.text}")


def delete_project_variable(
    host: str,
    token: str,
    project_id: int,
    key: str,
    environment_scope: str = "*",
    timeout_s: int = 20,
) -> None:
    key_norm = str(key).strip()
    if not key_norm:
        return

    key_enc = quote(key_norm, safe="")
    base_url = _url(host, f"/projects/{project_id}/variables/{key_enc}")
    headers = _headers(token)
    env_scope = str(environment_scope or "*").strip() or "*"
    params = {"filter[environment_scope]": env_scope}

    try:
        r = requests.delete(base_url, headers=headers, params=params, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e

    if r.status_code in (200, 204):
        return
    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code == 403:
        raise _scope_error(host)
    if r.status_code == 404:
        return

    raise GitLabError(f"Failed to delete variable '{key_norm}' (HTTP {r.status_code}).")