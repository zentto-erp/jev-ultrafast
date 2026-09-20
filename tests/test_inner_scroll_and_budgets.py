"""Reaching what does not fit on one screen.

A layout that fills the viewport never scrolls the page, so the page-level
scroll action is never offered — while a grid inside it holds a hundred rows
behind its own scrollbar. Everything past the fold is dropped for being
off-screen and the run reports one screenful as if it were all there is.

These tests are offline: no browser, no network, no paid API. They read the
observation source and exercise the Python that surrounds it.
"""

import importlib
import json
import re
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"


def snapshot_source():
    return SNAPSHOT.read_text(encoding="utf-8")


def reload_browser(monkeypatch, **env):
    for key in ("JEV_MAX_ACTIONS", "JEV_MAX_TEXT", "JEV_MAX_SCROLLERS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import jev_ultrafast.browser as browser
    return importlib.reload(browser)


# ─── The observation offers a way into every scrollable container ───────────

def test_scrollable_containers_become_actions():
    """Without this the agent can only move the page, which often cannot move."""
    source = snapshot_source()
    assert "scrollables" in source
    assert "scroll_in_" in source


def test_a_container_qualifies_by_measurement_not_by_looks():
    """`overflow:auto` alone is not enough — the content has to actually exceed it."""
    source = snapshot_source()
    assert "scrollHeight>e.clientHeight" in source
    assert "scrollWidth>e.clientWidth" in source


def test_innermost_container_is_offered_first():
    """Scrolling the outer shell when the grid holds the rows moves the wrong thing."""
    source = snapshot_source()
    ordering = re.search(r"\.sort\(\(a,b\)=>\(a\.r\.width\*a\.r\.height\)-\(b\.r\.width\*b\.r\.height\)\)", source)
    assert ordering, "containers must be sorted smallest-area first"


def test_page_scroll_is_not_replaced():
    """On an ordinary document the page scroll is still the right move."""
    source = snapshot_source()
    assert "id:'scroll_down'" in source
    assert "id:'scroll_up'" in source


def test_a_container_at_its_end_is_not_offered():
    """Offering a move that cannot happen teaches the agent that scrolling does nothing."""
    source = snapshot_source()
    assert "e.scrollTop+e.clientHeight<e.scrollHeight-2" in source
    assert "e.scrollLeft+e.clientWidth<e.scrollWidth-2" in source


def test_each_container_is_named_by_what_it_holds():
    """Two anonymous "Scroll down" choices are the same choice to the model."""
    source = snapshot_source()
    assert "'Scroll down inside '" in source


# ─── Crossing shadow boundaries when looking for the owning row ─────────────

def test_row_lookup_crosses_shadow_boundaries():
    """`closest` stops at the boundary, so a nested control never finds its row."""
    source = snapshot_source()
    assert "closestDeep" in source
    assert "root.host" in source
    assert "closestDeep(e,'tr,[role=\"row\"]')" in source


def test_closest_deep_stops_at_the_document():
    """Walking up must terminate; only a ShadowRoot yields a host to continue from."""
    source = snapshot_source()
    assert "node=root instanceof ShadowRoot ? root.host : null" in source


def test_guard_scope_also_crosses_boundaries():
    """The guard compares a scope; if it stops early it compares the wrong thing."""
    source = snapshot_source()
    assert "closestDeep(e,'form,dialog," in source


# ─── Budgets are policy, not a property of the engine ──────────────────────

def test_defaults_are_unchanged(monkeypatch):
    """Nobody who never sets a variable sees different behaviour."""
    browser = reload_browser(monkeypatch)
    assert browser.snapshot_budgets() == {}
    assert "window.__jevBudgets={}" in browser.READ_STATE


def test_caller_can_raise_the_caps(monkeypatch):
    """A dense table legitimately needs more than 250 actions."""
    browser = reload_browser(monkeypatch, JEV_MAX_ACTIONS="600", JEV_MAX_TEXT="20000")
    assert browser.snapshot_budgets() == {"actions": 600, "text": 20000}
    injected = json.loads(re.search(r"window\.__jevBudgets=(\{.*?\});", browser.READ_STATE).group(1))
    assert injected["actions"] == 600


@pytest.mark.parametrize("value", ["0", "-5", "", "many", "12.5"])
def test_an_unusable_value_is_ignored_not_obeyed(monkeypatch, value):
    """A typo must not silently remove a cap."""
    browser = reload_browser(monkeypatch, JEV_MAX_ACTIONS=value)
    assert "actions" not in browser.snapshot_budgets()


def test_the_script_reads_the_injected_budget():
    """The default has to survive when nothing is injected."""
    source = snapshot_source()
    assert "window.__jevBudgets?.[name]" in source
    assert "budget('actions',250)" in source
    assert "budget('text',6000)" in source


def test_budget_rejects_unusable_values_in_the_page_too():
    """The guard exists on both sides: the page may be handed anything."""
    source = snapshot_source()
    assert "Number.isFinite(value) && value>0" in source


# ─── Executing a scroll aimed at a container ───────────────────────────────

def test_container_scroll_is_aimed_not_guessed(monkeypatch):
    """A wheel at a fixed coordinate moves whatever happens to sit there."""
    browser = reload_browser(monkeypatch)
    source = Path(browser.__file__).read_text(encoding="utf-8")
    assert "e.scrollTop=before+action.delta" in source
    assert "e.scrollLeft=before+action.delta" in source


def test_a_scroll_that_did_not_move_is_reported(monkeypatch):
    """Silence would read as success and the agent would believe the list ended."""
    browser = reload_browser(monkeypatch)
    source = Path(browser.__file__).read_text(encoding="utf-8")
    assert "moved:after!==before" in source
    assert "already at that end" in source


def test_page_scroll_still_takes_the_wheel_path(monkeypatch):
    """Without a node there is no element to ask; the wheel is correct there."""
    browser = reload_browser(monkeypatch)
    source = Path(browser.__file__).read_text(encoding="utf-8")
    assert 'if node is None:' in source
    assert 'type="mouseWheel"' in source


def test_a_node_that_is_not_an_integer_is_refused(monkeypatch):
    """Node ids are code-owned; a model-supplied selector must never reach the page."""
    browser = reload_browser(monkeypatch)
    source = Path(browser.__file__).read_text(encoding="utf-8")
    assert "Invalid observed node" in source


# ─── The observation stays syntactically whole ─────────────────────────────

def test_snapshot_is_one_expression():
    """It is evaluated as a single expression; unbalanced braces break every run."""
    source = snapshot_source().strip()
    assert source.startswith("(() => {")
    assert source.endswith("})()")
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        assert source.count(opening) == source.count(closing), f"unbalanced {opening}{closing}"
