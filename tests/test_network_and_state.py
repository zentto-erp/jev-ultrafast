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
