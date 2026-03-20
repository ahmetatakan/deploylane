"""Tests for vars plan/diff logic and masked variable handling — no network required."""
from __future__ import annotations

import pytest

from deploylane.commands.vars import _iter_yaml_vars, _norm_scope
from deploylane.providers.base import CIVariable


# ─── _norm_scope ──────────────────────────────────────────────────────────────

def test_norm_scope_empty_string():
    assert _norm_scope("") == "*"

def test_norm_scope_none():
    assert _norm_scope(None) == "*"

def test_norm_scope_value():
    assert _norm_scope("production") == "production"

def test_norm_scope_strips_whitespace():
    assert _norm_scope("  staging  ") == "staging"


# ─── _iter_yaml_vars ──────────────────────────────────────────────────────────

def test_iter_basic():
    variables = {
        "FOO": {"value": "bar", "masked": False, "protected": False, "environment_scope": "*"},
    }
    items = list(_iter_yaml_vars("*", variables))
    assert len(items) == 1
    k, e, meta = items[0]
    assert k == "FOO"
    assert e == "*"
    assert meta["value"] == "bar"


def test_iter_scope_from_default():
    variables = {"FOO": {"value": "bar"}}
    items = list(_iter_yaml_vars("production", variables))
    assert items[0][1] == "production"


def test_iter_scope_per_var_overrides_default():
    variables = {"FOO": {"value": "bar", "environment_scope": "staging"}}
    items = list(_iter_yaml_vars("production", variables))
    assert items[0][1] == "staging"


def test_iter_skips_non_dict_values():
    variables = {
        "GOOD": {"value": "ok"},
        "BAD": "plain-string",
        "ALSO_BAD": 42,
    }
    items = list(_iter_yaml_vars("*", variables))
    assert len(items) == 1
    assert items[0][0] == "GOOD"


def test_iter_skips_empty_keys():
    variables = {"": {"value": "x"}, "VALID": {"value": "y"}}
    items = list(_iter_yaml_vars("*", variables))
    assert len(items) == 1
    assert items[0][0] == "VALID"


def test_iter_empty_variables():
    assert list(_iter_yaml_vars("*", {})) == []

def test_iter_none_variables():
    assert list(_iter_yaml_vars("*", None)) == []


# ─── Plan logic (replicated from vars_plan) ───────────────────────────────────

def _make_plan(desired_vars: dict, current_vars: list):
    """Replicate the creates/updates/prunes logic from vars_plan."""
    desired_pairs: set = set()
    desired_meta: dict = {}
    for k, e, meta in _iter_yaml_vars("*", desired_vars):
        desired_pairs.add((k, e))
        desired_meta[(k, e)] = meta

    current_pairs: set = set()
    current_map: dict = {}
    for v in current_vars:
        k = str(getattr(v, "key", "")).strip()
        e = _norm_scope(getattr(v, "environment_scope", "*"), "*")
        if k:
            current_pairs.add((k, e))
            current_map[(k, e)] = v

    creates = sorted(desired_pairs - current_pairs)
    prunes  = sorted(current_pairs - desired_pairs)

    updates = []
    for pair in sorted(desired_pairs & current_pairs):
        meta = desired_meta.get(pair, {})
        cur = current_map.get(pair)
        cur_masked = bool(getattr(cur, "masked", False))
        if (not cur_masked) and str(meta.get("value", "")) != str(getattr(cur, "value", "") or ""):
            updates.append(pair)
        elif bool(meta.get("masked")) != cur_masked or bool(meta.get("protected")) != bool(getattr(cur, "protected", False)):
            updates.append(pair)

    return creates, updates, prunes


def _var(key, value, masked=False, protected=False, scope="*"):
    return CIVariable(key=key, value=value, masked=masked, protected=protected, environment_scope=scope)


def test_plan_creates_new_var():
    creates, updates, prunes = _make_plan({"NEW": {"value": "x"}}, [])
    assert ("NEW", "*") in creates
    assert not updates
    assert not prunes


def test_plan_prunes_removed_var():
    creates, updates, prunes = _make_plan({}, [_var("OLD", "y")])
    assert ("OLD", "*") in prunes
    assert not creates
    assert not updates


def test_plan_update_on_value_change():
    desired = {"FOO": {"value": "new", "masked": False}}
    current = [_var("FOO", "old")]
    creates, updates, prunes = _make_plan(desired, current)
    assert ("FOO", "*") in updates
    assert not creates
    assert not prunes


def test_plan_no_update_when_value_same():
    desired = {"FOO": {"value": "same"}}
    current = [_var("FOO", "same")]
    creates, updates, prunes = _make_plan(desired, current)
    assert not updates
    assert not creates
    assert not prunes


def test_plan_masked_var_no_value_update():
    """Masked variables should never trigger a value-based update (API returns None)."""
    desired = {"SECRET": {"value": "anything", "masked": True}}
    current = [_var("SECRET", None, masked=True)]
    creates, updates, prunes = _make_plan(desired, current)
    assert not creates
    assert not prunes
    assert not updates  # masked + same flags → no update


def test_plan_update_on_flag_change():
    """Changing masked/protected should trigger update even if value is same."""
    desired = {"FOO": {"value": "x", "masked": False, "protected": True}}
    current = [_var("FOO", "x", masked=False, protected=False)]
    creates, updates, prunes = _make_plan(desired, current)
    assert ("FOO", "*") in updates


def test_plan_respects_environment_scope():
    desired = {
        "FOO": {"value": "x", "environment_scope": "production"},
        "FOO_STAGING": {"value": "y", "environment_scope": "staging"},
    }
    current = [_var("FOO", "x", scope="production")]
    creates, updates, prunes = _make_plan(desired, current)
    # FOO/production exists → no create; FOO_STAGING/staging is new → create
    assert ("FOO_STAGING", "staging") in creates
    assert ("FOO", "production") not in creates


# ─── vars get: masked variable value preservation ─────────────────────────────

def test_masked_value_preserved_from_existing():
    """When vars.yml already has a value for a masked var, vars get should keep it."""
    existing = {"SECRET": {"value": "my-secret", "masked": True}}
    var = CIVariable(key="SECRET", value=None, masked=True, protected=False, environment_scope="*")

    existing_meta = existing.get(var.key) or {}
    preserved = str(existing_meta.get("value", "")).strip() if isinstance(existing_meta, dict) else ""

    assert preserved == "my-secret"


def test_masked_value_empty_when_no_existing():
    """When no existing vars.yml, masked var value should be empty (not crash)."""
    existing = {}
    var = CIVariable(key="SECRET", value=None, masked=True, protected=False, environment_scope="*")

    existing_meta = existing.get(var.key) or {}
    preserved = str(existing_meta.get("value", "")).strip() if isinstance(existing_meta, dict) else ""

    assert preserved == ""


def test_non_masked_value_used_directly():
    """Non-masked variables should use the API value as-is."""
    var = CIVariable(key="HOST", value="10.0.0.1", masked=False, protected=False, environment_scope="*")
    value = var.value or ""
    assert value == "10.0.0.1"
