"""Contratos del alcance de la observacion: acotar, excluir, y decirlo.

Sin navegador: se comprueba la expresion que se manda y quien la manda, que es
donde estan las decisiones. Que el acotado funcione DENTRO de la pagina lo prueba
`scripts/check_guards.py` con Chrome de verdad.

Las tres cosas que pueden salir mal y que cada test fija:

- Partir la observacion en dos evaluaciones —una para fijar el alcance y otra
  para leer— abre una rendija en la que la pagina puede cambiar, y el resultado
  seria un estado que no existio nunca. La observacion es UNA lectura.
- Un alcance que se quede pegado de la observacion anterior hace que el paso
  siguiente decida sobre un trozo de pantalla que nadie pidio.
- Pedir una parte y recibir la pantalla entera en silencio es peor que no poder
  acotar, porque no hay nada en la salida que lo delate.
"""

import json
from unittest.mock import Mock

from jev_ultrafast.browser import READ_STATE, browser_operation, read_state_script

# Lo minimo que la observacion devuelve: la huella se calcula sobre estas claves,
# asi que una pagina falsa mas pobre falla en el calculo y no en lo que se prueba.
OBSERVED = {"url": "https://erp.test/grid", "title": "Rejilla", "text": "",
            "actions": [], "scroll": {"y": 0, "height": 800}, "marker": "m",
            "page_key": [], "guards": {}, "blocked": [], "keys": [],
            "omitted_actions": 0}


def test_la_constante_no_acota_nada():
    """`READ_STATE` es el caso normal y tiene que dejar el alcance en blanco."""
    assert "window.__jevScope=null" in READ_STATE
    assert "window.__jevIgnore=[]" in READ_STATE


def test_el_alcance_viaja_dentro_de_la_expresion():
    """Dentro, no en una evaluacion previa: la observacion es una sola lectura."""
    source = read_state_script("#grid", ["nav", ".banner"])
    assert json.dumps("#grid") + ";" in source.replace(" ", "")
    assert "window.__jevIgnore=" + json.dumps(["nav", ".banner"]) in source
    # Y sigue llevando los topes: acotar no sustituye al presupuesto.
    assert "window.__jevBudgets=" in source


def test_observar_sigue_siendo_una_sola_lectura_del_navegador(monkeypatch):
    """El invariante que protege el acotado de romper la atomicidad."""
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": dict(OBSERVED)}})
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "observe", "session": "t", "screenshot": False,
                       "scope": "#grid", "ignore": ["nav"]})
    assert cdp.call_count == 1, "acotar no puede costar una evaluacion extra"
    expression = cdp.call_args.kwargs["expression"]
    assert "#grid" in expression and "nav" in expression


def test_sin_acotar_se_usa_la_constante(monkeypatch):
    """Para no recomponer un script de cientos de lineas en el caso normal."""
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": dict(OBSERVED)}})
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "observe", "session": "t", "screenshot": False})
    assert cdp.call_args.kwargs["expression"] == READ_STATE


def test_una_observacion_sin_alcance_lo_deja_en_blanco(monkeypatch):
    """Un alcance pegado del paso anterior no se ve en ninguna salida."""
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": dict(OBSERVED)}})
    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({"operation": "observe", "session": "t", "screenshot": False})
    assert "window.__jevScope=null" in cdp.call_args.kwargs["expression"]


def test_el_recorrido_hereda_su_alcance_en_cada_observacion(monkeypatch):
    """Si el alcance va en la llamada, la observacion que alguien olvide acotar
    devuelve la pantalla entera y deshace el acotado sin avisar."""
    import jev_ultrafast.browser as browser

    llamadas = []

    def recorder(method, **params):
        llamadas.append(params.get("expression", ""))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if "readyState" in expression:
                return {"result": {"value": "complete"}}
            if "innerHTML.length" in expression:
                return {"result": {"value": 100}}
            return {"result": {"value": dict(OBSERVED)}}
        return {}

    monkeypatch.setattr(browser, "cdp", recorder)
    monkeypatch.setattr(browser, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser.time, "sleep", lambda _s: None)

    b = browser.Browser(None, reuse_target="prestada", scope="#grid", ignore=["nav"])
    b.observe(screenshot=False)
    acotadas = [e for e in llamadas if "#grid" in e]
    assert acotadas, "la observacion del recorrido tiene que llevar su alcance"
