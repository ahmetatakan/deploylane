from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from .base import CIVariable, Project, ProviderError, User


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_base(host: str) -> str:
    """Return the API base URL for a given GitHub host."""
    h = (host or "https://github.com").rstrip("/")
    if h in ("https://github.com", "http://github.com", "github.com"):
        return "https://api.github.com"
    # GitHub Enterprise Server: {host}/api/v3
    return f"{h}/api/v3"


def _url(host: str, path: str) -> str:
    return f"{_api_base(host)}{path}"


def _unreachable_error(host: str, cause: Exception) -> ProviderError:
    return ProviderError(
        f"Cannot reach GitHub at {host}\n"
        f"  Check the host URL and your network connection.\n"
        f"  Cause: {cause}"
    )


def _auth_error(host: str) -> ProviderError:
    return ProviderError(
        f"Authentication failed for {host}\n"
        f"  Your token may be invalid or expired.\n"
        f"  Fix: GitHub → Settings → Developer settings → Personal access tokens\n"
        f"  Required scopes: 'repo' (classic) or fine-grained with Secrets + Variables write\n"
        f"  Then run: dlane login --host {host}"
    )


def _forbidden_error(resource: str) -> ProviderError:
    return ProviderError(
        f"Access denied to {resource}.\n"
        f"  Required: 'repo' scope or fine-grained token with Actions secrets + variables permissions."
    )


def _get(host: str, token: str, path: str, params: Optional[Dict] = None, timeout_s: int = 15) -> requests.Response:
    url = _url(host, path)
    try:
        r = requests.get(url, headers=_headers(token), params=params, timeout=timeout_s)
    except requests.RequestException as e:
        raise _unreachable_error(host, e) from e
    if r.status_code == 401:
        raise _auth_error(host)
    if r.status_code == 403:
        raise _forbidden_error(path)
    if r.status_code == 404:
        raise ProviderError(f"Not found: {path}")
    if r.status_code >= 400:
        raise ProviderError(f"GitHub API error (HTTP {r.status_code}).")
    return r


# ─── Secret encryption ────────────────────────────────────────────────────────

def _encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Encrypt a secret value using the repo's public key (libsodium sealed box)."""
    try:
        from nacl import encoding, public as nacl_public
    except ImportError:
        raise ProviderError(
            "GitHub secret encryption requires PyNaCl.\n"
            "  Install: pip install 'deploylane[github]'\n"
            "  Or directly: pip install PyNaCl"
        )
    pub_key = nacl_public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    box = nacl_public.SealedBox(pub_key)
    encrypted = box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


# ─── Provider ─────────────────────────────────────────────────────────────────

class GitHubProvider:
    """GitHub Actions provider.

    Project IDs use owner/repo format (e.g. 'acme/gateway').
    masked=True  → GitHub Secret  (encrypted at rest, value not readable via API)
    masked=False → GitHub Variable (plain text, readable via API)
    environment_scope='*' → repo-level; any other value → GitHub Environment name.
    """

    def __init__(self, host: str, token: str) -> None:
        if not host or host.rstrip("/") in ("https://github.com", "http://github.com", "github.com"):
            self._host = "https://github.com"
        else:
            self._host = host.rstrip("/")
        self._token = token

    # ── Core ──────────────────────────────────────────────────────────────────

    def whoami(self) -> User:
        r = _get(self._host, self._token, "/user")
        data = r.json()
        return User(
            id=int(data.get("id", 0)),
            username=str(data.get("login", "")),
            name=str(data.get("name") or data.get("login", "")),
        )

    def list_projects(
        self,
        search: Optional[str] = None,
        owned: bool = False,
        membership: bool = True,
    ) -> List[Project]:
        if search:
            q = f"{search} in:name"
            if owned:
                q += " user:@me"
            r = _get(self._host, self._token, "/search/repositories",
                     params={"q": q, "per_page": 100})
            return [_project_from(item) for item in r.json().get("items", [])]

        params: Dict[str, Any] = {"per_page": 100, "page": 1}
        if owned:
            params["type"] = "owner"
        elif membership:
            params["affiliation"] = "owner,collaborator,organization_member"

        out: List[Project] = []
        while True:
            r = _get(self._host, self._token, "/user/repos", params=params)
            items = r.json()
            if not isinstance(items, list) or not items:
                break
            out.extend(_project_from(item) for item in items)
            if 'rel="next"' not in r.headers.get("Link", ""):
                break
            params["page"] += 1

        return out

    def get_project(self, path: str) -> Project:
        enc = quote(path, safe="/")
        r = _get(self._host, self._token, f"/repos/{enc}")
        return _project_from(r.json())

    # ── Variables & Secrets ───────────────────────────────────────────────────

    def list_variables(self, project_id: str) -> List[CIVariable]:
        """List repo-level secrets (masked=True, value=None) and variables (masked=False)."""
        path = project_id  # owner/repo
        out: List[CIVariable] = []

        # Secrets — values are never returned by GitHub API
        try:
            r = _get(self._host, self._token,
                     f"/repos/{path}/actions/secrets", params={"per_page": 100})
            for s in r.json().get("secrets", []):
                out.append(CIVariable(
                    key=s["name"],
                    value=None,
                    protected=False,
                    masked=True,
                    environment_scope="*",
                ))
        except ProviderError:
            pass  # token lacks secret read access — skip silently

        # Variables — values are readable
        try:
            r = _get(self._host, self._token,
                     f"/repos/{path}/actions/variables", params={"per_page": 100})
            for v in r.json().get("variables", []):
                out.append(CIVariable(
                    key=v["name"],
                    value=v.get("value", ""),
                    protected=False,
                    masked=False,
                    environment_scope="*",
                ))
        except ProviderError:
            pass  # token lacks variable read access — skip silently

        out.sort(key=lambda v: (v.key.lower(), v.environment_scope))
        return out

    def set_variable(
        self,
        project_id: str,
        key: str,
        value: str,
        protected: bool = False,
        masked: bool = False,
        environment_scope: str = "*",
        variable_type: str = "env_var",
    ) -> None:
        path = project_id
        if masked:
            self._upsert_secret(path, key, value, environment_scope)
        else:
            self._upsert_variable(path, key, value, environment_scope)

    def delete_variable(
        self,
        project_id: str,
        key: str,
        environment_scope: str = "*",
    ) -> None:
        path = project_id
        key_enc = quote(key, safe="")

        if environment_scope != "*":
            candidates = [
                f"/repos/{path}/environments/{environment_scope}/secrets/{key_enc}",
                f"/repos/{path}/environments/{environment_scope}/variables/{key_enc}",
            ]
        else:
            candidates = [
                f"/repos/{path}/actions/secrets/{key_enc}",
                f"/repos/{path}/actions/variables/{key_enc}",
            ]

        for url_path in candidates:
            url = _url(self._host, url_path)
            try:
                r = requests.delete(url, headers=_headers(self._token), timeout=20)
            except requests.RequestException as e:
                raise _unreachable_error(self._host, e) from e
            if r.status_code in (200, 204):
                return
            if r.status_code == 401:
                raise _auth_error(self._host)
            if r.status_code == 403:
                raise _forbidden_error(url_path)
            # 404 → not in this endpoint, try next

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _upsert_secret(self, path: str, key: str, value: str, environment_scope: str) -> None:
        # 1) Fetch repo public key
        if environment_scope != "*":
            pk_path = f"/repos/{path}/environments/{environment_scope}/secrets/public-key"
        else:
            pk_path = f"/repos/{path}/actions/secrets/public-key"

        pk_r = _get(self._host, self._token, pk_path)
        pk_data = pk_r.json()
        encrypted = _encrypt_secret(pk_data["key"], value)

        # 2) PUT secret
        key_enc = quote(key, safe="")
        if environment_scope != "*":
            secret_path = f"/repos/{path}/environments/{environment_scope}/secrets/{key_enc}"
        else:
            secret_path = f"/repos/{path}/actions/secrets/{key_enc}"

        url = _url(self._host, secret_path)
        try:
            r = requests.put(
                url, headers=_headers(self._token),
                json={"encrypted_value": encrypted, "key_id": pk_data["key_id"]},
                timeout=20,
            )
        except requests.RequestException as e:
            raise _unreachable_error(self._host, e) from e

        if r.status_code == 401:
            raise _auth_error(self._host)
        if r.status_code == 403:
            raise _forbidden_error(f"secret '{key}'")
        if r.status_code not in (201, 204):
            raise ProviderError(f"Failed to set secret '{key}' (HTTP {r.status_code}).")

    def _upsert_variable(self, path: str, key: str, value: str, environment_scope: str) -> None:
        if environment_scope != "*":
            base_path = f"/repos/{path}/environments/{environment_scope}/variables"
        else:
            base_path = f"/repos/{path}/actions/variables"

        key_enc = quote(key, safe="")

        # Try PATCH (update existing)
        patch_url = _url(self._host, f"{base_path}/{key_enc}")
        try:
            r = requests.patch(
                patch_url, headers=_headers(self._token),
                json={"name": key, "value": value},
                timeout=20,
            )
        except requests.RequestException as e:
            raise _unreachable_error(self._host, e) from e

        if r.status_code in (200, 204):
            return
        if r.status_code == 401:
            raise _auth_error(self._host)
        if r.status_code == 403:
            raise _forbidden_error(f"variable '{key}'")

        # 404 → create
        if r.status_code == 404:
            post_url = _url(self._host, base_path)
            try:
                r2 = requests.post(
                    post_url, headers=_headers(self._token),
                    json={"name": key, "value": value},
                    timeout=20,
                )
            except requests.RequestException as e:
                raise _unreachable_error(self._host, e) from e
            if r2.status_code in (201, 204):
                return
            if r2.status_code == 401:
                raise _auth_error(self._host)
            if r2.status_code == 403:
                raise _forbidden_error(f"variable '{key}'")
            raise ProviderError(f"Failed to create variable '{key}' (HTTP {r2.status_code}).")

        raise ProviderError(f"Failed to update variable '{key}' (HTTP {r.status_code}).")


# ─── Utility ──────────────────────────────────────────────────────────────────

def _project_from(data: Dict[str, Any]) -> Project:
    return Project(
        id=str(data["full_name"]),  # owner/repo — used as project_id in all API calls
        path=str(data["full_name"]),
        web_url=data.get("html_url"),
        default_branch=data.get("default_branch"),
    )
