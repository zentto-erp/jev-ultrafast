"""The scripts this engine injects have to parse.

A syntax error in one of them does not fail loudly: the whole script is
discarded before a line runs, so everything it installs is simply absent. That
reads as the page not doing what it should, not as the driver being broken —
the exact confusion this engine exists to remove.

It happened: adding network recording to the dialog script reused a name the
dialog half had already declared, and the result was `armed: false` with no
error anywhere. The parse check would have caught it in a second.

Offline: node only, no browser and no network.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "jev_ultrafast"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


def arm_source():
    """`_ARM_SOURCE` with its placeholders filled the way the engine fills them."""
    text = (ROOT / "browser.py").read_text(encoding="utf-8")
    body = re.search(r'_ARM_SOURCE = """(.*?)"""', text, re.S)
    assert body, "browser.py no longer defines _ARM_SOURCE"
    return body.group(1) % ("true", '""')


def parses(source, tmp_path, name):
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return subprocess.run([NODE, "--check", str(target)],
                          capture_output=True, text=True, timeout=30)


def test_the_armed_script_parses(tmp_path):
    result = parses(arm_source(), tmp_path, "arm.js")
    assert result.returncode == 0, result.stderr


def test_the_snapshot_parses(tmp_path):
    result = parses((ROOT / "snapshot.js").read_text(encoding="utf-8"), tmp_path, "snapshot.js")
    assert result.returncode == 0, result.stderr


def test_the_armed_script_declares_each_name_once(tmp_path):
    """The failure that motivated this file: two halves of one script, each
    declaring a helper with the same name. Node reports it as a redeclaration,
    so a deliberately broken copy must still be rejected."""
    broken = arm_source() + "\nconst noteCall = 1;\nconst noteCall = 2;\n"
    result = parses(broken, tmp_path, "broken.js")
    assert result.returncode != 0
    assert "already been declared" in result.stderr
