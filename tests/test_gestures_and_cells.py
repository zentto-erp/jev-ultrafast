"""Gestures the page actually listens for.

Applications hang real behaviour on a held key and on a second click. A grid
that edits in place opens its editor on double click, so the whole capture flow
of a document — the lines, the quantities, the prices — cannot be driven with
single clicks. A feature whose entire point is the key being down cannot be
verified by pressing and releasing it.

Offline: no browser, no network, no paid API.
"""

import importlib
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"


def snapshot_source():
    return SNAPSHOT.read_text(encoding="utf-8")


def browser():
    import jev_ultrafast.browser as module
    return importlib.reload(module)


def browser_source():
    return Path(browser().__file__).read_text(encoding="utf-8")


# ─── Modifiers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("names,expected", [
    (None, 0), ([], 0),
    (["alt"], 1), (["ctrl"], 2), (["meta"], 4), (["shift"], 8),
    (["ctrl", "alt"], 3), (["ctrl", "shift"], 10), (["alt", "ctrl", "shift"], 11),
])
def test_modifier_mask_matches_cdp_bits(names, expected):
    assert browser().modifier_mask(names) == expected


@pytest.mark.parametrize("name,expected", [
    ("cmd", 4), ("command", 4), ("option", 1), ("opt", 1), ("CTRL", 2), (" Ctrl ", 2),
])
def test_the_names_people_use_are_accepted(name, expected):
    """`cmd` and `option` are the Mac names for keys that already have a bit."""
    assert browser().modifier_mask([name]) == expected


def test_a_single_name_does_not_have_to_be_a_list():
    assert browser().modifier_mask("shift") == 8


def test_an_unknown_modifier_is_refused_not_ignored():
    """Silently dropping it reports a pass for a gesture that never happened."""
    with pytest.raises(ValueError, match="Unknown modifier"):
        browser().modifier_mask(["hyper"])


def test_a_held_key_is_always_released():
    """A stuck Ctrl poisons every later step, and looks like the page misbehaving."""
    source = browser_source()
    assert source.count("finally:") >= 2
    assert "for name, code in reversed(pressed):" in source


# ─── Holding a key long enough to see what it does ─────────────────────────

def test_hold_observes_while_the_key_is_down():
    """Press and release with nothing between observes the page as it was."""
    source = browser_source()
    hold = source.split('if operation == "hold"')[1].split('if operation == "act"')[0]
    assert "rawKeyDown" in hold
    assert "evaluate(READ_STATE)" in hold
    assert hold.index("rawKeyDown") < hold.index("evaluate(READ_STATE)")


def test_hold_waits_before_observing():
    """The shortcut hint waits 450 ms so it does not flash on every copy-paste."""
    source = browser_source()
    hold = source.split('if operation == "hold"')[1].split('if operation == "act"')[0]
    assert "time.sleep" in hold
    assert 'request.get("settle", 0.8)' in hold


def test_hold_refuses_to_hold_nothing():
    source = browser_source()
    hold = source.split('if operation == "hold"')[1].split('if operation == "act"')[0]
    assert "Nothing to hold" in hold


def test_hold_releases_even_when_observation_fails():
    """Otherwise a stale page leaves the key down for the rest of the run."""
    source = browser_source()
    hold = source.split('if operation == "hold"')[1].split('if operation == "act"')[0]
    assert "finally:" in hold
    assert hold.index("finally:") < hold.index("if seen is None")


# ─── The second click ──────────────────────────────────────────────────────

def test_double_click_sends_a_rising_count():
    """Chrome wants the full sequence, not one event with clickCount 2."""
    source = browser_source()
    assert 'if kind == "dblclick":' in source
    assert "for count in (1, 2):" in source


def test_cells_are_offered_for_editing():
    """A cell is not an input until the second click creates one."""
    source = snapshot_source()
    assert "kind:'dblclick'" in source
    assert "td,th,[role=\"gridcell\"]" in source


def test_a_cell_holding_a_control_is_driven_through_that_control():
    source = snapshot_source()
    third_pass = source.split("Third pass")[1]
    assert "if (e.querySelector(selector)) continue;" in third_pass


def test_a_cell_is_named_by_its_row():
    """A hundred cells reading "12,00" are a hundred indistinguishable targets."""
    source = snapshot_source()
    third_pass = source.split("Third pass")[1]
    assert "closestDeep(e,'tr,[role=\"row\"]')" in third_pass


def test_an_empty_cell_is_not_offered():
    source = snapshot_source()
    third_pass = source.split("Third pass")[1]
    assert "if (!own) continue;" in third_pass


def test_snapshot_is_still_one_balanced_expression():
    source = snapshot_source().strip()
    assert source.startswith("(() => {") and source.endswith("})()")
    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        assert source.count(opening) == source.count(closing), f"unbalanced {opening}{closing}"


# ─── Controls that are visible but not yet usable ──────────────────────────

def test_disabled_controls_are_reported_not_dropped():
    """The confirm button of a picker is disabled until a row is ticked.

    An agent that cannot see it concludes there is no way to confirm, and takes
    Cancel — or Create new, which is worse.
    """
    source = snapshot_source()
    assert "const blocked=[];" in source
    assert "blocked.push(" in source


def test_blocked_controls_are_not_offered_as_actions():
    """They cannot be pressed; offering them would be a step that always fails."""
    source = snapshot_source()
    disabled_branch = source.split("const blocked=[];")[1].split("const r=e.getBoundingClientRect(), x=")[0]
    assert "continue;" in disabled_branch
    assert "actions.push" not in disabled_branch


def test_aria_disabled_counts_as_disabled():
    """A styled button is not a <button disabled>; it says so with aria."""
    source = snapshot_source()
    assert "e.getAttribute('aria-disabled')==='true'" in source


def test_blocked_is_returned_to_the_caller():
    """Collecting it and not reporting it would help nobody."""
    source = snapshot_source()
    assert "actions,blocked," in source


def test_blocked_has_its_own_budget():
    """A form mid-fill can have dozens; they must not crowd out the page text."""
    source = snapshot_source()
    assert "MAX_BLOCKED=budget('blocked',12)" in source
    assert "blocked.length<MAX_BLOCKED" in source
