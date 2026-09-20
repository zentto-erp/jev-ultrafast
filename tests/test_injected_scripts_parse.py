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

import importlib
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


def test_the_expression_declared_actually_builds_parses(tmp_path):
    """El JS que vive dentro de un metodo se inyecta igual y se rompe igual.

    Paso dos veces en el mismo dia, y la segunda estaba EN UN COMENTARIO: una
    secuencia de escape escrita dentro de una cadena de Python la interpreta
    Python primero, asi que a la pagina llega un salto de linea real que parte
    el comentario y deja media frase como codigo suelto. El sintoma fue un
    SyntaxError en la pagina, lejos de donde estaba el error.

    Y este test comprueba la expresion QUE SE ENVIA, construida por el propio
    metodo. Mi primer intento la sacaba del fuente con una expresion regular
    no-greedy, que cortaba en el primer `})()` y validaba un trozo que
    casualmente parseaba: daba verde con el fallo dentro.
    """
    import jev_ultrafast.browser as module
    importlib.reload(module)
    enviado = {}

    def espia(self, expression):
        enviado["js"] = expression
        return None

    original = module.Browser._await
    module.Browser._await = espia
    try:
        fake = module.Browser.__new__(module.Browser)
        fake.evaluate = lambda expression: True      # modelContext presente
        module.Browser.declared(fake, "una_herramienta", None)
    finally:
        module.Browser._await = original

    assert enviado.get("js"), "declared() ya no pasa por _await — revisa este test"
    result = parses(enviado["js"], tmp_path, "declared.js")
    assert result.returncode == 0, result.stderr


def every_injected_literal():
    """Cada cadena triple que browser.py manda a la pagina como JS.

    No solo las que tienen nombre. La tercera vez que un escape de Python se
    coló en un literal de JS fue dentro del bloque que resuelve el objetivo de
    cada accion — que no era `_ARM_SOURCE` ni `snapshot.js`, asi que los dos
    tests anteriores daban verde mientras TODOS los clicks fallaban.
    """
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    for match in re.finditer(r'(?:evaluate|expression=)\("""(.*?)"""', source, re.S):
        body = match.group(1)
        if "=>" not in body and "(" not in body:
            continue
        # Los huecos que Python rellena en tiempo de ejecucion: el `%s` de las
        # plantillas y el argumento que se concatena tras la cadena.
        filled = body.replace("%s", "({})")
        if filled.rstrip().endswith("("):
            filled = filled.rstrip() + "{})"
        yield match.start(), filled


def test_every_javascript_literal_sent_to_the_page_parses(tmp_path):
    """Un error de sintaxis aqui no falla con ruido: el script entero se
    descarta antes de correr una linea, asi que la operacion simplemente no
    hace nada y el informe culpa a la pagina.

    Medido: un `split` con un salto de linea escapado dejo `act` roto, y con el
    TODOS los clicks del motor, mientras los 258 tests seguian en verde.
    """
    checked = 0
    for at, body in every_injected_literal():
        result = parses(body, tmp_path, f"literal{at}.js")
        assert result.returncode == 0, f"el literal en el offset {at} no parsea:\n{result.stderr}"
        checked += 1
    assert checked >= 3, f"solo se comprobaron {checked} literales — la extraccion se quedo corta"
