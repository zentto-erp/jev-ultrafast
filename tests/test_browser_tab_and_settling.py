"""Offline contracts for tab reuse and for waiting until a page settles.

No browser and no paid APIs: CDP is replaced by a recorder so the tests pin the
decisions, not Chrome's behaviour.
"""

import pytest

from jev_ultrafast import browser as browser_mod
from jev_ultrafast.browser import Browser


class FakeCdp:
    """Records CDP calls and answers the handful the constructor makes."""

    def __init__(self, sizes=(100, 100, 100)):
        self.calls = []
        self.created = 0
        self.closed = []
        self._sizes = list(sizes)

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "Target.createTarget":
            self.created += 1
            return {"targetId": "new-target"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Target.closeTarget":
            self.closed.append(params.get("targetId"))
            return {}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if "readyState" in expression:
                return {"result": {"value": "complete"}}
            if "innerHTML.length" in expression:
                value = self._sizes.pop(0) if self._sizes else 100
                return {"result": {"value": value}}
        return {}


@pytest.fixture
def fake(monkeypatch):
    recorder = FakeCdp()
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)
    return recorder


def test_opens_its_own_tab_by_default(fake):
    """Unchanged behaviour: a run with no tab given opens one."""
    b = Browser("https://example.test/")
    assert fake.created == 1
    assert b.target == "new-target"
    assert b.owns_target is True


def test_reuses_the_given_tab_instead_of_opening_one(fake):
    """Watching several runs in a row should not scatter them across tabs."""
    b = Browser("https://example.test/", reuse_target="existing-tab")
    assert fake.created == 0
    assert b.target == "existing-tab"
    assert b.owns_target is False


def test_closing_only_closes_a_tab_it_opened(fake):
    """A borrowed tab is not ours to close: the caller is still using it."""
    borrowed = Browser("https://example.test/", reuse_target="existing-tab")
    borrowed.close()
    assert fake.closed == []

    owned = Browser("https://example.test/")
    owned.close()
    assert fake.closed == ["new-target"]


def test_navigates_the_reused_tab(fake):
    """Reusing a tab still has to take it to the requested URL."""
    Browser("https://example.test/somewhere", reuse_target="existing-tab")
    navigations = [p for m, p in fake.calls if m == "Page.navigate"]
    assert navigations and navigations[0]["url"] == "https://example.test/somewhere"


def test_waits_for_the_dom_to_stop_changing(monkeypatch):
    """`readyState == complete` is not "the page is ready" on a SPA.

    The document finishes while the framework is only just taking over, so
    observing there returns the empty shell and an assertion fails against a
    page the user never saw. The wait must outlast a DOM that is still growing.
    """
    sizes = [0, 0, 500, 900, 1400, 1400, 1400, 1400]
    recorder = FakeCdp(sizes=sizes)
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)

    Browser("https://example.test/")

    # It kept looking while the size moved, and only returned once it repeated.
    sizes_read = [
        p for m, p in recorder.calls
        if m == "Runtime.evaluate" and "innerHTML.length" in p.get("expression", "")
    ]
    assert len(sizes_read) >= 5


def test_an_empty_body_never_counts_as_settled(monkeypatch):
    """Size 0 is the shell, not a stable page: returning there is the bug."""
    recorder = FakeCdp(sizes=[0, 0, 0, 0, 0, 0, 700, 700, 700])
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)

    Browser("https://example.test/")

    reads = [
        p for m, p in recorder.calls
        if m == "Runtime.evaluate" and "innerHTML.length" in p.get("expression", "")
    ]
    assert len(reads) > 5
