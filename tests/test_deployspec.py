"""Tests for deploy.yml parsing — no SSH or network required."""
from __future__ import annotations

import pytest

from deploylane.deployspec import load_deployspec


def _write(tmp_path, content: str):
    p = tmp_path / "deploy.yml"
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_bluegreen(tmp_path):
    p = _write(tmp_path, """
project: acme/gateway
strategy: bluegreen
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/gateway
    ports:
      blue: 8080
      green: 8081
""")
    spec = load_deployspec(p)
    assert spec.project == "acme/gateway"
    t = spec.targets["prod"]
    assert t.host == "10.0.0.1"
    assert t.strategy == "bluegreen"
    assert t.port_blue == 8080
    assert t.port_green == 8081


def test_valid_plain(tmp_path):
    p = _write(tmp_path, """
project: acme/api
strategy: plain
targets:
  staging:
    host: 192.168.1.1
    user: deploy
    deploy_dir: /home/deploy/api
""")
    spec = load_deployspec(p)
    assert spec.project == "acme/api"
    assert "staging" in spec.targets
    assert spec.targets["staging"].strategy == "plain"


def test_per_target_strategy_overrides_top(tmp_path):
    p = _write(tmp_path, """
project: acme/app
strategy: plain
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/app
    strategy: bluegreen
""")
    spec = load_deployspec(p)
    assert spec.targets["prod"].strategy == "bluegreen"


def test_nested_ports(tmp_path):
    p = _write(tmp_path, """
project: acme/app
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/app
    ports:
      blue: 9000
      green: 9001
""")
    spec = load_deployspec(p)
    assert spec.targets["prod"].port_blue == 9000
    assert spec.targets["prod"].port_green == 9001


def test_flat_port_format(tmp_path):
    p = _write(tmp_path, """
project: acme/app
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/app
    port_blue: 7000
    port_green: 7001
""")
    spec = load_deployspec(p)
    assert spec.targets["prod"].port_blue == 7000
    assert spec.targets["prod"].port_green == 7001


def test_multiple_targets(tmp_path):
    p = _write(tmp_path, """
project: acme/app
default_target: prod
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/app
  staging:
    host: 10.0.0.2
    user: deploy
    deploy_dir: /home/deploy/app-staging
""")
    spec = load_deployspec(p)
    assert set(spec.targets.keys()) == {"prod", "staging"}
    assert spec.default_target == "prod"


def test_missing_project_raises(tmp_path):
    p = _write(tmp_path, """
targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/app
""")
    with pytest.raises(ValueError, match="project"):
        load_deployspec(p)


def test_missing_targets_raises(tmp_path):
    p = _write(tmp_path, "project: acme/app\n")
    with pytest.raises(ValueError, match="targets"):
        load_deployspec(p)


def test_target_missing_host_is_skipped(tmp_path):
    """A target with missing host is silently skipped — raises if no valid targets remain."""
    p = _write(tmp_path, """
project: acme/app
targets:
  bad:
    user: deploy
    deploy_dir: /home/deploy/app
""")
    with pytest.raises(ValueError, match="targets"):
        load_deployspec(p)


def test_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_deployspec(tmp_path / "nonexistent.yml")


def test_empty_file_raises(tmp_path):
    p = tmp_path / "deploy.yml"
    p.write_text("", encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        load_deployspec(p)
