# DeployLane (dlane)

GitLab-focused deployment helper CLI for **deterministic CI/CD
operations**.

DeployLane helps you manage GitLab project variables and deployment
workflows in a reproducible and auditable way — without committing
secrets into repositories.

------------------------------------------------------------------------

## 🚀 What is DeployLane?

DeployLane is a CLI tool that sits between your workstation and GitLab.

It allows you to:

-   🔐 Store GitLab credentials locally (not in repo)
-   📦 Export project variables into a YAML file
-   🔍 Diff local YAML vs GitLab variables
-   📋 Generate deterministic deployment plans
-   🧾 Produce reproducible `.env` files
-   🧮 Generate deployment proof manifests (hash-based audit artifacts)

All operations are deterministic and reproducible.

> ⚠️ `.deploylane/` is local-only. Do NOT commit it.

------------------------------------------------------------------------

# 📦 Installation

``` bash
pip install deploylane
```

Run:

``` bash
dlane --help
```

------------------------------------------------------------------------

# 🔐 Authentication

## Create a GitLab Personal Access Token (PAT)

In GitLab:

User Settings → Access Tokens

Scopes:

-   `read_api`
-   `api`

------------------------------------------------------------------------

## Login (interactive)

``` bash
dlane login --host https://gitlab.example.com
```

Optional:

``` bash
dlane login --profile prod --host https://gitlab.example.com --registry-host registry.example.com
```

------------------------------------------------------------------------

## Login (non-interactive)

``` bash
export GITLAB_HOST="https://gitlab.example.com"
export GITLAB_TOKEN="glpat-xxxx"

dlane login --non-interactive
```

------------------------------------------------------------------------

## Status

``` bash
dlane status
```

------------------------------------------------------------------------

# ⚙️ Profiles

Config file:

    ~/.config/deploylane/config.toml

Commands:

``` bash
dlane config show
dlane profile list
dlane profile use <name>
```

------------------------------------------------------------------------

# 📁 Projects

``` bash
dlane project list
dlane project list --search my-app
dlane project list --owned
```

------------------------------------------------------------------------

# 🔑 Variables

Default location:

    .deploylane/vars.yml

Export:

``` bash
dlane vars get --project group/project
```

Plan:

``` bash
dlane vars plan --file .deploylane/vars.yml
```

Diff:

``` bash
dlane vars diff --project group/project
```

Apply:

``` bash
dlane vars apply --file .deploylane/vars.yml
```

Prune:

``` bash
dlane vars prune --file .deploylane/vars.yml --yes
```

------------------------------------------------------------------------

# 🚀 Deployment

Plan:

``` bash
dlane deploy plan --target prod
```

Render:

``` bash
dlane deploy render --target prod
```

Proof:

``` bash
dlane deploy proof --target prod
```

------------------------------------------------------------------------

# 🔐 Security

Add to `.gitignore`:

    .deploylane/
    *.env
    .env

------------------------------------------------------------------------

# 🛠 Development

``` bash
pip install build
python -m build
```

Dev install:

``` bash
pip install -e ".[dev]"
```
