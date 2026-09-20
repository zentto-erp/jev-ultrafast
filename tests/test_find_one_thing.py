"""Asking for one thing instead of reading the whole screen.

An observation of a dense screen is a few hundred actions and thousands of
characters. A step that only needs to know "is there a Confirm button and can I
press it" pays for all of it — and pays again on every later turn, because the
answer stays in the conversation. One careless observation on a long run is
charged hundreds of times over.

Measured against the real picker dialog: 27.403 characters for the observation,
359 for the same question asked directly. 76 times less, and the short answer
was the more useful one — it carried the button, its blocked state, and the
line of page text that says what to do about it.

Offline: no browser, no network.
"""

import importlib


def step():
    import jev_ultrafast.step as module
    return importlib.reload(module)


def page(actions=(), blocked=(), keys=(), text=""):
    return {"url": "https://example.test/x", "actions": list(actions),
            "blocked": list(blocked), "keys": list(keys), "text": text}


def test_find_is_a_real_operation():
    import inspect
    source = inspect.getsource(step().main)
    assert '"observe", "keys", "find",' in source
    assert 'args.operation == "find"' in source


def test_it_looks_in_all_three_places_a_control_can_be():
    """A find that answers "no" because the button is disabled, or because it
    is only bound to a key, teaches the run the wrong lesson."""
    import inspect
    body = inspect.getsource(step().main)
    branch = body[body.index('if args.operation == "find":'):]
    for where in ('page["actions"]', 'page.get("blocked"', 'page.get("keys"'):
        assert where in branch, where


def test_it_returns_the_pages_own_words_too():
    """For what is read rather than pressed: a total, an error, a status."""
    import inspect
    body = inspect.getsource(step().main)
    branch = body[body.index('if args.operation == "find":'):]
    assert 'page.get("text")' in branch
    # One line either side, which is what tells "Saldo 0,00" from "Saldo" as a
    # column heading.
    assert "at - 1" in branch and "at + 2" in branch


def test_found_is_false_only_when_nothing_matched_anywhere():
    """Not "no clickable control": a disabled button is still an answer, and a
    run that treats it as absent goes looking for a screen that is not there."""
    import inspect
    body = inspect.getsource(step().main)
    branch = body[body.index('if args.operation == "find":'):]
    assert '"found": bool(hits or stopped or keys or around)' in branch


def test_the_label_matcher_still_prefers_an_exact_hit():
    """`find()` the helper is a different thing from the `find` operation, and
    the helper's rule matters: "Guardar" must not resolve to "Guardar Compra"
    when both are on screen."""
    both = page(actions=[{"id": "e1", "kind": "click", "label": "Guardar Compra", "node": 1},
                         {"id": "e2", "kind": "click", "label": "Guardar", "node": 2}])
    assert step().find(both, "Guardar")["node"] == 2
