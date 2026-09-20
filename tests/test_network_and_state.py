"""Seeing the calls, and not having to log in again.

Two capabilities that decide whether a run can tell "the application failed"
from "the driver failed", and whether it can start at the screen under test at
all.

Offline: these assert on the shipped source and on node, never on a browser.
"""

import re
from pathlib import Path

BROWSER = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "browser.py"


def source():
    return BROWSER.read_text(encoding="utf-8")


def branch(name):
    text = source()
    start = text.index(f'if operation == "{name}":')
    rest = text[start + 10:]
    end = rest.index("if operation ==")
    return text[start:start + 10 + end]


def test_the_network_trail_is_the_browsers_own_record():
    """Wrapping fetch and XMLHttpRequest was written first and measured: on a
    real screen it caught 4 calls out of 38, missing every one the application
    depended on. A wrapper only sees what goes through the function it
    replaced."""
    where = branch("network")
    assert "performance.getEntriesByType('resource')" in where
    assert "responseStatus" in where


def test_nothing_is_patched_into_the_page_for_it():
    """Perturbing what you are measuring is how a driver invents defects."""
    text = source()
    assert "window.fetch =" not in text
    assert "XMLHttpRequest.prototype.open =" not in text
    assert "XMLHttpRequest.prototype.send =" not in text


def test_a_request_with_no_answer_counts_as_failed():
    """A refused connection, a CORS rejection or a dead name resolves to status
    0 or to nothing at all — and those leave the screen emptiest of all, so
    treating them as fine would hide the worst case."""
    where = branch("network")
    assert "status === null || status === 0 || status >= 400" in where


def test_only_failures_unless_everything_is_asked_for():
    """A screen makes forty calls in a step; listing all of them buries the one
    that matters under thirty-nine that worked."""
    assert 'if not request.get("all")' in branch("network")


def test_the_session_is_saved_whole():
    """Cookies alone are not a session in an application that keeps anything in
    a store — it restores, looks logged in, and then behaves like a stranger."""
    where = branch("state")
    assert "Network.getCookies" in where
    assert "localStorage" in where and "sessionStorage" in where


def test_a_stores_entries_go_back_only_to_their_own_origin():
    """Writing one site's tokens into another is how a restore turns into a
    leak."""
    assert 'origin["origin"] != evaluate("location.origin")' in branch("state")


def test_a_target_id_cannot_name_another_folder():
    """It arrives from the caller and it names a file."""
    import importlib
    module = importlib.import_module("jev_ultrafast.browser")
    assert module._arm_record("../../etc/passwd").name == "etcpasswd.json"
    assert module._arm_record("") is None


def test_arming_is_remembered_so_a_navigation_does_not_undo_it():
    """The registration belongs to a CDP session and this engine is one process
    per step, so it dies with the process. Observed: armed, navigated, and then
    armed reported false."""
    text = source()
    assert "remember_arm(" in text and "recall_arm(" in text
    assert "def prearm_next_document" in text
    # And it has to happen BEFORE the navigation, or the calls made while the
    # page was loading — most of them — happened with nothing listening.
    assert re.search(r"def navigate.*?\n.*?self\.prearm_next_document\(\)", text, re.S)


def snapshot_text():
    return (BROWSER.parent / "snapshot.js").read_text(encoding="utf-8")


def test_a_blocked_control_can_carry_its_reason():
    """Knowing a control is disabled turns a dead end into "do something else
    first". Knowing WHICH something else turns it into the next step."""
    text = snapshot_text()
    assert "const whyBlocked=" in text
    assert "validationMessage" in text
    assert "aria-errormessage" in text
    assert "fieldset[disabled]" in text


def test_the_title_is_not_a_reason():
    """Measured on the real picker: the pagination buttons returned "Primera
    fila" and "Bloque anterior" as their reason for being disabled. That is not
    a reason, it is the button's name — and a confident wrong reason is worse
    than none, because it sends the run somewhere specific that is not there."""
    text = snapshot_text()
    start = text.index("const whyBlocked=")
    body = text[start:text.index("for (const e of crossRoots(selector))", start)]
    assert "getAttribute('title')" not in body


def test_no_reason_is_reported_when_the_page_gives_none():
    """Saying nothing is the honest answer, and it is also the signal that the
    application should publish one."""
    text = snapshot_text()
    assert "return null;\n  };" in text[text.index("const whyBlocked="):]
    assert "blocked.push(why ? {role:role(e),label:label.slice(0,80),why}" in text


def test_each_refusal_to_act_says_which_refusal_it_is():
    """They all used to come back as one null, and one null became "Target
    changed or is covered" — a sentence that names two very different
    situations and is wrong about at least one of them every time."""
    text = source()
    for reason in ("gone from the document", "disabled now", "no longer visible",
                   "read-only now", "scrolled out of the viewport", "covered by"):
        assert reason in text, reason
