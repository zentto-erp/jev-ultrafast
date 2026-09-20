"""Nothing on the page should be out of reach.

Half of an application is not built from buttons. A card, a row, a tile or a
chip is a div with a click handler: no role, no tabindex, nothing the standard
selector matches — so it does not exist for the agent, which then reports that
the list cannot be opened. Measured on a data grid whose mobile view is cards:
every card a plain div, none of them reachable.

The same applies to what lives one document down: a same-origin iframe is a
document too, and a report preview rendered there read as a blank screen.

Offline: no browser, no network, no paid API.
"""

import importlib
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"


def snapshot_source():
    return SNAPSHOT.read_text(encoding="utf-8")


def browser_source():
    import jev_ultrafast.browser as browser
    importlib.reload(browser)
    return Path(browser.__file__).read_text(encoding="utf-8")


# ─── Elements that behave like controls without being one ──────────────────

def test_clickable_elements_without_a_role_are_offered():
    """A card is a div; without this the agent cannot open a single one."""
    source = snapshot_source()
    assert "looksClickable" in source
    assert "cursor==='pointer'" in source


def test_declared_intent_also_counts():
    """`tabindex`, `onclick` and row/listitem roles are the author saying so."""
    source = snapshot_source()
    assert "hasAttribute('onclick')" in source
    assert "hasAttribute('tabindex')" in source
    assert "'row','listitem','treeitem','article'" in source


def test_a_control_is_not_offered_twice():
    """It is already in the first pass; a duplicate is a second way to say the same."""
    source = snapshot_source()
    assert "if (e.matches(selector)) return false;" in source


def test_only_the_outermost_of_a_pointer_group_is_offered():
    """A parent painted with pointer makes every child look clickable."""
    source = snapshot_source()
    assert "getComputedStyle(parent).cursor==='pointer'" in source


def test_a_container_holding_controls_is_not_a_target():
    """Scaffolding; the controls inside are the real intent and already offered."""
    source = snapshot_source()
    assert "if (e.querySelector(selector)) return false;" in source


def test_an_unnameable_target_is_skipped():
    """Without text the model cannot choose it on purpose."""
    source = snapshot_source()
    assert "if (!own) continue;" in source


def test_geometry_is_checked_before_computed_style():
    """getComputedStyle on thousands of nodes is the expensive call; a rect is free."""
    source = snapshot_source()
    second_pass = source.split("Second pass")[1]
    rect_at = second_pass.index("getBoundingClientRect")
    style_at = second_pass.index("looksClickable")
    assert rect_at < style_at, "reject by rectangle before asking for style"


# ─── One document down ─────────────────────────────────────────────────────

def test_same_origin_frames_are_walked():
    """A report preview renders there; without this it reads as a blank screen."""
    source = snapshot_source()
    assert "querySelectorAll('iframe,frame')" in source
    assert "contentDocument" in source


def test_a_cross_origin_frame_is_stepped_over_quietly():
    """Access throws by design; that is a wall, not a failure to report."""
    source = snapshot_source()
    assert "try { inner=f.contentDocument; } catch { inner=null; }" in source


def test_frame_text_counts_as_page_text():
    """Reaching the controls but not the words would still leave it half-blind."""
    source = snapshot_source()
    text_roots = source.split("const textRoots")[1].split("const actions")[0]
    assert "contentDocument" in text_roots


# ─── Asking instead of acting ──────────────────────────────────────────────

def inspect_block():
    """Just the inspect branch.

    Cut at the NEXT `if operation`, whatever it is — cutting at `act` assumed
    the two were adjacent, and the moment another operation was added in
    between, this test started reading somebody else's mouse events.
    """
    source = browser_source()
    body = source.split('if operation == "inspect"')[1]
    following = body.find("\n    if operation")
    return body[:following] if following != -1 else body


def test_inspect_exists_and_is_read_only():
    """Verifying must not change what is being verified."""
    inspect = inspect_block()
    assert "getBoundingClientRect" in inspect
    for mutation in ("Input.dispatchMouseEvent", "Page.navigate", "insertText", ".click()"):
        assert mutation not in inspect, f"inspect must not {mutation}"


def test_inspect_refuses_anything_but_an_observed_node():
    """Node ids are code-owned; a model-supplied selector must never reach the page."""
    inspect = inspect_block()
    assert "if type(node) is not int:" in inspect
    assert "Invalid observed node" in inspect


def test_inspect_reports_a_vanished_element_instead_of_guessing():
    """Silence would let a check pass against an element that is no longer there."""
    inspect = inspect_block()
    assert "StalePage" in inspect


def test_inspect_returns_more_than_the_observation_carries():
    """Its reason to exist is the value the capped observation left out."""
    inspect = inspect_block()
    for field in ("text", "value", "checked", "disabled", "visible", "attributes"):
        assert f"{field}:" in inspect


def test_snapshot_is_still_one_balanced_expression():
    """Evaluated as a single expression; unbalanced braces break every run."""
    source = snapshot_source().strip()
    assert source.startswith("(() => {") and source.endswith("})()")
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        assert source.count(opening) == source.count(closing), f"unbalanced {opening}{closing}"
