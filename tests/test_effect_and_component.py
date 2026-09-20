"""Verifying by effect, and reaching the state that has no control.

"The click did not raise an error" is the weakest claim a run can make.
Components say what happened — they emit CustomEvents that cross the shadow
boundary — and they hold state in JS properties that no gesture can set.

Offline: no browser, no network, no paid API.
"""

import importlib
from pathlib import Path

import pytest


def browser():
    import jev_ultrafast.browser as module
    return importlib.reload(module)


def source():
    return Path(browser().__file__).read_text(encoding="utf-8")


def block(name):
    """One operation branch, cut at the next one."""
    body = source().split(f'if operation == "{name}"')[1]
    following = body.find("\n    if operation")
    return body[:following] if following != -1 else body


# ─── Listening for what the component says ─────────────────────────────────

def test_listening_captures_events_across_the_shadow_boundary():
    """The events worth hearing are `composed`, so they arrive at the document."""
    listen = block("listen")
    assert "document.addEventListener" in listen


def test_listening_replaces_the_previous_subscription():
    """Otherwise handlers pile up and one gesture is counted many times."""
    listen = block("listen")
    assert "for (const off of box.stop) off();" in listen


def test_event_names_are_required_and_checked():
    listen = block("listen")
    assert "Pass `events` as a list" in listen


def test_event_detail_is_recorded_never_executed():
    """It is data from the page; it came from somewhere we do not control."""
    listen = block("listen")
    assert "JSON.stringify" in listen
    for danger in ("eval(", "new Function", "innerHTML"):
        assert danger not in listen


def test_recorded_events_are_capped():
    """One chatty event must not fill the observation."""
    listen = block("listen")
    assert "box.seen.length > 50" in listen


def test_hearing_nothing_is_an_answer():
    """Silence means the component did not consider anything to have happened."""
    heard = block("heard")
    assert "?? []" in heard


# ─── Reaching state that no gesture can set ────────────────────────────────

def test_component_access_takes_a_name_not_an_expression():
    """The page must never run text written by a model."""
    component = block("component")
    assert "member_name(member)" in component
    for danger in ("eval(", "new Function"):
        assert danger not in component


def test_component_access_refuses_an_unobserved_node():
    component = block("component")
    assert "if type(node) is not int:" in component


def test_only_three_modes_are_allowed():
    component = block("component")
    assert '("get", "set", "call")' in component


def test_an_unknown_member_is_reported_not_guessed():
    """Reading `undefined` off a typo would look like an empty value."""
    component = block("component")
    assert "if (!(req.member in e)) return {unknown:true};" in component


def test_a_throwing_member_is_surfaced():
    """A method that raised is not the same as a method that returned nothing."""
    component = block("component")
    assert "catch (err) return" in component or "{failed:" in component
    assert "raised:" in component


def test_returned_values_are_serialised_and_capped():
    component = block("component")
    assert "JSON.stringify(value ?? null)" in component
    assert ".slice(0, 8000)" in component


@pytest.mark.parametrize("bad", [
    "a b", "x;y", "", "2fast", "a.b", "getLayout()", None, 42,
    "__proto__", "constructor", "prototype",
])
def test_a_member_name_that_is_not_a_plain_name_is_refused(bad):
    """It travels as a property name, so anything else is refused before it goes."""
    with pytest.raises(ValueError):
        browser().member_name(bad)


@pytest.mark.parametrize("good", ["getLayout", "rows", "_privateish", "tasks2"])
def test_a_plain_name_is_accepted(good):
    assert browser().member_name(good) == good
