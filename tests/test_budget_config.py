"""The action budget is a policy, not a property of the engine."""

import importlib
import os

import pytest


def reload_questions(monkeypatch, **env):
    for key in ("JEV_MAX_STEPS", "JEV_MAX_MODEL_CALLS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import jev_ultrafast.questions as q
    return importlib.reload(q)


def test_default_is_unchanged(monkeypatch):
    """Nobody who never sets the variable sees different behaviour."""
    q = reload_questions(monkeypatch)
    assert q.MAX_STEPS == 60
    assert q.MAX_MODEL_CALLS == 120


def test_caller_can_raise_the_ceiling(monkeypatch):
    """A picker with a hundred rows legitimately needs more than 60 actions."""
    q = reload_questions(monkeypatch, JEV_MAX_STEPS="250")
    assert q.MAX_STEPS == 250
    assert q.MAX_MODEL_CALLS == 500


def test_model_calls_can_be_set_apart(monkeypatch):
    q = reload_questions(monkeypatch, JEV_MAX_STEPS="100", JEV_MAX_MODEL_CALLS="130")
    assert q.MAX_STEPS == 100
    assert q.MAX_MODEL_CALLS == 130


@pytest.mark.parametrize("value", ["0", "-5", "muchos", ""])
def test_unusable_values_fall_back_to_the_default(monkeypatch, value):
    """A typo must not silently remove the ceiling: that is what the ceiling
    is protecting against."""
    q = reload_questions(monkeypatch, JEV_MAX_STEPS=value)
    assert q.MAX_STEPS == 60
