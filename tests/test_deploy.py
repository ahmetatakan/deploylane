"""Tests for deploy push/pull logic — SSH calls are mocked."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from deploylane.commands.deploy import _parse_env, _compose_diff, _push_project
from deploylane.remote import RemoteError
from deploylane.deploystate import write_state, local_file_hashes


# ─── _parse_env ───────────────────────────────────────────────────────────────

def test_parse_env_basic(tmp_path):
    f = tmp_path / "test.env"
    f.write_text("FOO=bar\nBAZ=qux\n")
    assert _parse_env(f) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_ignores_comments(tmp_path):
    f = tmp_path / "test.env"
    f.write_text("# comment\nKEY=value\n")
    result = _parse_env(f)
    assert result == {"KEY": "value"}


def test_parse_env_ignores_empty_lines(tmp_path):
    f = tmp_path / "test.env"
    f.write_text("\n\nKEY=value\n\n")
    assert _parse_env(f) == {"KEY": "value"}


def test_parse_env_empty_value(tmp_path):
    f = tmp_path / "test.env"
    f.write_text("EMPTY=\n")
    assert _parse_env(f) == {"EMPTY": ""}


def test_parse_env_value_with_equals(tmp_path):
    """Values containing = should be preserved after the first =."""
    f = tmp_path / "test.env"
    f.write_text("URL=postgres://user:pass@host/db?ssl=true\n")
    result = _parse_env(f)
    assert result["URL"] == "postgres://user:pass@host/db?ssl=true"


# ─── _compose_diff ────────────────────────────────────────────────────────────

def test_compose_diff_identical():
    content = "services:\n  app:\n    image: test\n"
    assert _compose_diff(content, content) == []


def test_compose_diff_detects_change():
    remote = "services:\n  app:\n    image: old:1.0\n"
    local  = "services:\n  app:\n    image: new:2.0\n"
    diff = _compose_diff(remote, local)
    assert any("old:1.0" in line for line in diff)
    assert any("new:2.0" in line for line in diff)


def test_compose_diff_fromfile_tofile_labels():
    diff = _compose_diff("a\n", "b\n")
    header = "".join(diff[:2])
    assert "server:docker-compose.yml" in header
    assert "local:docker-compose.yml" in header


# ─── _push_project pull-first protection ─────────────────────────────────────

def _setup_workspace(tmp_path: Path, strategy: str = "plain") -> Path:
    """Create a minimal workspace + project structure for push tests.

    Layout:
      tmp_path/workspace.yml      ← ws_path (ws_path.parent = tmp_path)
      tmp_path/myapp/.deploylane/ ← project root
    """
    ws_file = tmp_path / "workspace.yml"
    ws_file.write_text(f"""\
version: 1
projects:
  - name: myapp
    path: myapp
    gitlab_project: acme/myapp
    strategy: {strategy}
""")

    proj = tmp_path / "myapp" / ".deploylane"
    proj.mkdir(parents=True)

    (proj / "deploy.yml").write_text(f"""\
project: acme/myapp
strategy: {strategy}
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/myapp
""")

    env_dir = proj / "env"
    env_dir.mkdir()
    (env_dir / "prod.env").write_text("APP_NAME=myapp\n")

    compose_dir = proj / "compose"
    compose_dir.mkdir()
    (compose_dir / f"{strategy}.yml").write_text(LOCAL_COMPOSE)

    # Write a pull state so push tests that run with yes=True pass the
    # pull-before-push check by default (tests needing no-state can skip this).
    hashes = local_file_hashes(proj, strategy, "docker-compose.yml", "deploy.sh")
    write_state(proj, "prod", "10.0.0.1", hashes)

    return ws_file


LOCAL_COMPOSE = "services:\n  app:\n    image: myapp:local\n"
REMOTE_COMPOSE_DIFF = "services:\n  app:\n    image: myapp:OLD\n"


def test_push_blocks_when_compose_differs(tmp_path):
    """push should return (False, ...) when server compose differs and --force is not set."""
    ws_path = _setup_workspace(tmp_path)

    def mock_read(dest, path):
        if "docker-compose" in path:
            return REMOTE_COMPOSE_DIFF
        raise RemoteError("not found")

    with patch("deploylane.commands.deploy.remote_file_exists", return_value=False), \
         patch("deploylane.commands.deploy.read_remote_file", side_effect=mock_read), \
         patch("deploylane.commands.deploy.copy_file"):

        ok, err, _ = _push_project(ws_path, "myapp", "prod", yes=True, force=False)

    assert not ok
    assert "pull" in err.lower() or "force" in err.lower()


def test_push_succeeds_with_force_when_compose_differs(tmp_path):
    """push should succeed with --force even when server compose differs."""
    ws_path = _setup_workspace(tmp_path)

    with patch("deploylane.commands.deploy.remote_file_exists", return_value=False), \
         patch("deploylane.commands.deploy.read_remote_file", return_value=REMOTE_COMPOSE_DIFF), \
         patch("deploylane.commands.deploy.copy_file"):

        ok, err, _ = _push_project(ws_path, "myapp", "prod", yes=True, force=True)

    assert ok
    assert err == ""


def test_push_succeeds_when_compose_matches(tmp_path):
    """push should succeed when server compose matches local."""
    ws_path = _setup_workspace(tmp_path)

    with patch("deploylane.commands.deploy.remote_file_exists", return_value=False), \
         patch("deploylane.commands.deploy.read_remote_file", return_value=LOCAL_COMPOSE), \
         patch("deploylane.commands.deploy.copy_file"):

        ok, err, _ = _push_project(ws_path, "myapp", "prod", yes=True, force=False)

    assert ok


def test_push_skips_env_when_exists_on_server(tmp_path):
    """.env must never be overwritten if it already exists on the server."""
    ws_path = _setup_workspace(tmp_path)

    def mock_file_exists(dest, path):
        return ".env" in path

    with patch("deploylane.commands.deploy.remote_file_exists", side_effect=mock_file_exists), \
         patch("deploylane.commands.deploy.read_remote_file", return_value=LOCAL_COMPOSE), \
         patch("deploylane.commands.deploy.copy_file") as mock_copy:

        ok, err, changes = _push_project(ws_path, "myapp", "prod", yes=True, force=False)

    copied_paths = [str(call) for call in mock_copy.call_args_list]
    assert not any(".env" in p for p in copied_paths)
    assert changes.get("env") == "skipped"


def test_push_unknown_target_returns_error(tmp_path):
    """push with a non-existent target should return (False, error)."""
    ws_path = _setup_workspace(tmp_path)

    with patch("deploylane.commands.deploy.remote_file_exists", return_value=False), \
         patch("deploylane.commands.deploy.read_remote_file", return_value=LOCAL_COMPOSE), \
         patch("deploylane.commands.deploy.copy_file"):

        ok, err, _ = _push_project(ws_path, "myapp", "nonexistent", yes=True)

    assert not ok
    assert "nonexistent" in err or "Unknown target" in err


def test_push_unknown_project_returns_error(tmp_path):
    """push with a project not in workspace.yml should return (False, error)."""
    ws_path = _setup_workspace(tmp_path)

    with patch("deploylane.commands.deploy.remote_file_exists", return_value=False), \
         patch("deploylane.commands.deploy.copy_file"):

        ok, err, _ = _push_project(ws_path, "doesnotexist", "prod", yes=True)

    assert not ok
    assert "doesnotexist" in err or "not found" in err.lower()
