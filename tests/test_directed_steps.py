"""The other door: steps a caller already knows how to name.

The loop asks a model what to do next. That is right when the move is a
judgement and wrong when it is written down — and for a while these operations
existed with nothing able to call them, which an agent correctly reported as
"the engine has no inspect mode". It had one. It had no door.

Offline: no browser, no network, no paid API.
"""

import importlib
from pathlib import Path

import pytest


def browser():
    import jev_ultrafast.browser as module
    return importlib.reload(module)


def step():
    import jev_ultrafast.step as module
    return importlib.reload(module)


# ─── Keys that are not text ────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected_key", [
    ("escape", "Escape"), ("esc", "Escape"), ("ESCAPE", "Escape"),
    ("enter", "Enter"), ("return", "Enter"), ("tab", "Tab"),
    ("f2", "F2"), ("F12", "F12"),
    ("up", "ArrowUp"), ("arrowdown", "ArrowDown"),
    ("delete", "Delete"), ("backspace", "Backspace"),
])
def test_named_keys_resolve(name, expected_key):
    key, _code, virtual = browser().key_spec(name)
    assert key == expected_key
    assert isinstance(virtual, int) and virtual > 0


def test_a_single_character_is_allowed():
    key, code, _virtual = browser().key_spec("a")
    assert key == "a" and code is None


def test_an_unknown_key_name_is_refused():
    """Sending "e" when the case asked for "escape" closes nothing and reads as broken."""
    with pytest.raises(ValueError, match="Unknown key"):
        browser().key_spec("escapee")


def test_key_press_carries_the_virtual_code():
    """Without it Chrome delivers an event the page does not recognise."""
    source = Path(browser().__file__).read_text(encoding="utf-8")
    block = source.split('if operation == "key"')[1].split("\n    if operation")[0]
    assert "windowsVirtualKeyCode" in block
    assert "nativeVirtualKeyCode" in block


def test_a_printable_key_carries_its_text():
    source = Path(browser().__file__).read_text(encoding="utf-8")
    block = source.split('if operation == "key"')[1].split("\n    if operation")[0]
    assert 'down["text"] = name' in block


def test_modifiers_are_released_after_a_key():
    source = Path(browser().__file__).read_text(encoding="utf-8")
    block = source.split('if operation == "key"')[1].split("\n    if operation")[0]
    assert "finally:" in block


# ─── Every capability has a door ───────────────────────────────────────────

@pytest.mark.parametrize("method", [
    "inspect", "listen", "heard", "component", "hold", "hover",
    "drag", "context_menu", "touch", "key",
])
def test_browser_exposes_the_operation(method):
    """Implemented in browser_operation and unreachable is the same as absent."""
    assert callable(getattr(browser().Browser, method, None)), f"Browser has no {method}()"


@pytest.mark.parametrize("operation", [
    "observe", "click", "dblclick", "fill", "inspect", "listen", "heard",
    "component", "hold", "hover", "drag", "contextmenu", "touch", "scroll", "key",
])
def test_the_directed_script_offers_the_operation(operation):
    source = Path(step().__file__).read_text(encoding="utf-8")
    assert f'"{operation}"' in source


# ─── Naming a control instead of guessing one ──────────────────────────────

def test_an_exact_label_wins_over_a_partial_one():
    page = {"actions": [
        {"id": "e1", "kind": "click", "label": "Guardar Compra", "node": 1},
        {"id": "e2", "kind": "click", "label": "Guardar", "node": 2},
    ]}
    assert step().find(page, "Guardar")["node"] == 2


def test_a_disabled_control_is_explained_not_reported_missing():
    """The commonest dead end: the button is there, waiting for something else."""
    page = {"actions": [], "blocked": [{"role": "button", "label": "Seleccionar"}]}
    with pytest.raises(SystemExit) as stop:
        step().find(page, "Seleccionar")
    assert "disabled" in str(stop.value.code)
    assert "enables it first" in str(stop.value.code)


def test_a_missing_control_lists_what_was_there():
    """A vague goal retried thirteen times is worse than one honest answer."""
    page = {"actions": [{"id": "e1", "kind": "click", "label": "Cancelar", "node": 1}], "blocked": []}
    with pytest.raises(SystemExit) as stop:
        step().find(page, "Confirmar")
    assert "not_found" in str(stop.value.code)
    assert "Cancelar" in str(stop.value.code)
