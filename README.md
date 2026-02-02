# DeployLane (dlane)

GitLab-focused deployment helper CLI for **deterministic** CI/CD operations:
- Store GitLab credentials locally (not in repo)
- List projects
- Manage **project variables** via a local YAML file (export/apply/diff)
- Keep infra workflows reproducible without coupling to Git state

> **Important:** `.deploylane/` is local-only. Do not commit it.

---

## What is DeployLane?

DeployLane is a small CLI that sits between your workstation and GitLab:
- You log in once (PAT stored in `~/.config/deploylane/config.toml`)
- You can export GitLab project variables into a local YAML file
- You can edit YAML, then apply changes back to GitLab deterministically
- You can diff local YAML vs GitLab before applying

This makes GitLab variables manageable as **files**, without placing secrets in app repos.

---

## Install

### From PyPI
```bash
pip install deploylane
```

### Run
```bash
dlane --help
```

---

## Authentication

### Create a GitLab Personal Access Token (PAT)
In GitLab UI:
- **User Settings → Access Tokens**
- Scopes (minimum recommended):
  - `read_api` (for listing projects, reading variables)
  - `api` (required if you want to set/update variables)

> If you only read, `read_api` can be enough. For writes, you usually need `api`.

### Login (interactive)
```bash
dlane login --host https://gitlab.example.com
```

If you omit `--host`, it uses the default or prompts.

### Login (non-interactive / CI-friendly)
```bash
export GITLAB_HOST="https://gitlab.example.com"
export GITLAB_TOKEN="glpat-xxxx"
dlane login --non-interactive
```

### Status / WhoAmI
```bash
dlane status
dlane whoami
```

### Logout
```bash
dlane logout
```

---

## Config & Profiles

DeployLane stores credentials locally:

- Config path:
  - `~/.config/deploylane/config.toml`
- Supports profiles (e.g. `default`, `staging`, `prod`)

Example:
```bash
dlane login --profile default --host https://gitlab.example.com
dlane login --profile corp --host https://gitlab.example.com
```

Switching active profile is handled by whichever profile was last set as active (per your CLI logic).

Debug:
```bash
dlane config-show
dlane host-normalize --host gitlab.example.com
```

---

## Projects

List projects visible to your token:

```bash
dlane projects-list
```

Search:
```bash
dlane projects-list --search <project>
```

Owned only:
```bash
dlane projects-list --owned
```

Membership filtering:
```bash
dlane projects-list --membership
dlane projects-list --no-membership
```

---

## Variables (Project-level)

DeployLane uses a **local YAML file** to manage GitLab project variables.

### Default file location
By default, you can keep it in the repository root but **not committed**:

- `.deploylane/vars.yml`

> This is local-only state. Add it to `.gitignore`.

### Export variables (GitLab → YAML)
```bash
dlane vars-get --project <group>/<project>
```

Write to a specific file:
```bash
dlane vars-get --project <group>/<project> --out .deploylane/vars.yml
```

If the project has no variables, DeployLane writes a **demo template** so you can start editing immediately.

### Diff (YAML vs GitLab)
```bash
dlane vars-diff --project <group>/<project>
```

Use a specific YAML file:
```bash
dlane vars-diff --project <group>/<project> --file .deploylane/vars.yml
```

### Apply (YAML → GitLab)
This will create/update variables defined in YAML.

```bash
dlane vars-apply --project <group>/<project>
```

Or:
```bash
dlane vars-apply --project <group>/<project> --file .deploylane/vars.yml
```

> Current behavior: applies all entries in YAML (create/update).  
> Deletions are intentionally **not** handled yet.

---

## YAML Format

Example `.deploylane/vars.yml`:

```yaml
project: <group>/<project>
scope: "*"
variables:
  PROD_HOST:
    value: 192.168.1.2
    masked: true
    protected: true
    environment_scope: "*"
  REGISTRY_USER:
    value: gitlab+deploy-token-1
    masked: true
    protected: true
    environment_scope: "*"
```

Notes:
- `scope: "*"` is currently informational (kept for future expansion).
- `environment_scope` controls GitLab variable environment targeting.

---

## Security Notes (Very important)

- Do **NOT** commit `.deploylane/vars.yml` if it contains secrets.
- Prefer storing sensitive values in GitLab variables and pulling them when needed.
- Local `.deploylane/` is for deterministic management, but it’s still sensitive.

Recommended `.gitignore`:
```gitignore
.deploylane/
*.env
.env
```

---

## Roadmap (next steps)

- Safer apply:
  - `--dry-run` (show changes without applying)
  - `--only KEY1,KEY2` (apply subset)
- Optional deletion flow:
  - explicit `vars-delete KEY` (single variable)
- Group/instance variable support (future)
- Deployment helpers (later):
  - ship compose / snippets as artifacts (Generic Packages)
  - deterministic server-side fetch + apply

---

## Development

Run locally:
```bash
dlane --help
```

Build (requires `build`):
```bash
pip install build
python -m build
```
