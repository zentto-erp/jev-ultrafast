"""The keys a screen offers, read off the screen.

A key is the cheapest way to drive an application: nothing to find, nothing to
hit-test, and it still works when the button is scrolled out of the viewport or
folded into a narrow toolbar. But WHICH keys exist belongs to the screen, not to
the app, so they are discovered rather than assumed — a table hardcoded in a
prompt goes stale the day one of them moves.

Offline: the real regex and the real budget are pulled out of snapshot.js and
run in node, so these assert on the shipped characters rather than on a copy.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"
NODE = shutil.which("node")


def snapshot_source():
    return SNAPSHOT.read_text(encoding="utf-8")


def key_regex_line():
    """The literal KEY line as shipped — not a re-typed copy of it."""
    for line in snapshot_source().splitlines():
        if line.strip().startswith("const KEY="):
            return line.strip()
    raise AssertionError("snapshot.js no longer declares KEY")


def test_keys_reach_the_caller():
    assert "actions,blocked,keys," in snapshot_source()


def test_keys_have_their_own_budget():
    """Without one they would compete with the action budget and vanish first."""
    assert "MAX_KEYS=budget('keys',16)" in snapshot_source()


def test_a_key_is_read_from_the_control_not_from_a_table():
    """Both conventions an app uses to show a shortcut: a kbd chip, and the
    title that becomes the tooltip. Neither is a list this engine maintains."""
    source = snapshot_source()
    assert "crossRoots('kbd')" in source
    assert "aria-keyshortcuts" in source


def test_a_disabled_control_marks_its_key_disabled():
    """Pressing an inert key looks like a broken application."""
    source = snapshot_source()
    assert "off?{key,label,disabled:true}:{key,label}" in source


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_shipped_regex_accepts_function_keys_and_rejects_prose():
    """A `kbd` chip can hold anything — "Ctrl", "⌘K", a stray word. Only the keys
    this engine can actually press should be reported as available."""
    program = key_regex_line() + """
    const cases=['F2','F9','F12','Esc','Escape','Enter','f4',
                 'Ctrl','K','Guardar','','F13','F0','Shift+F2'];
    console.log(JSON.stringify(cases.filter(c=>KEY.test(c))));
    """
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    accepted = set(json.loads(out.stdout))
    assert {"F2", "F9", "F12", "Esc", "Escape", "Enter", "f4"} <= accepted
    # F13 and F0 do not exist on a keyboard; a combination is not a bare key.
    assert accepted.isdisjoint({"Ctrl", "K", "Guardar", "", "F13", "F0", "Shift+F2"})
