"""Repetir un recorrido sin volver a preguntarle a un modelo.

La primera corrida de un caso es un juicio. La segunda no: el recorrido ya está
escrito, y volver a pagar inferencia para redescubrirlo es pagar dos veces por
la misma decisión — con el añadido de que un modelo puede elegir distinto y
convertir una regresión en un misterio.

Offline: sin navegador y sin red.
"""

import json

import pytest

import jev_ultrafast.replay as replay_module
from jev_ultrafast.replay import replay, script_from


class Navegador:
    """Lo justo para ejercitar el bucle: observa, actúa, y apunta lo hecho."""

    def __init__(self, paginas, falla_en=None):
        self.paginas = list(paginas)
        self.falla_en = falla_en
        self.hecho = []

    def observe(self, screenshot=False):
        return self.paginas[min(len(self.hecho), len(self.paginas) - 1)]

    def act(self, target, page, text=None):
        if self.falla_en == target.get("label"):
            # La MISMA clase que captura `replay`, no una importada de nuevo.
            # Otro test del conjunto recarga `jev_ultrafast.browser`, y tras un
            # reload la clase es otro objeto: el `except` no la reconoce y el
            # test falla solo cuando corre acompañado. El codigo de produccion
            # no recarga modulos; el fallo era del test.
            raise replay_module.StalePage('covered by "banner"')
        self.hecho.append((target["label"], text))


def buscar(page, text, kind=None):
    for accion in page["actions"]:
        if (accion.get("label") or "").lower() == text.lower():
            return accion
    raise SystemExit(json.dumps({"error": "not_found", "looked_for": text,
                                 "available": [a.get("label") for a in page["actions"]]}))


def pagina(*etiquetas):
    return {"actions": [{"id": f"e{n}", "kind": "click", "label": e, "node": n}
                        for n, e in enumerate(etiquetas, start=1)]}


def escribir(tmp_path, contenido):
    destino = tmp_path / "guion.json"
    destino.write_text(json.dumps(contenido), encoding="utf-8")
    return str(destino)


def test_un_summary_de_una_corrida_sirve_de_guion(tmp_path):
    """Lo grabado ya tiene la forma: etiqueta, tipo y lo que se escribió."""
    donde = escribir(tmp_path, {"history": [
        {"step": 1, "action": "Nueva Compra", "kind": "click", "text": None},
        {"step": 2, "action": "Número", "kind": "fill", "text": "F-1"},
    ]})
    assert script_from(donde) == [
        {"kind": "click", "label": "Nueva Compra", "text": None},
        {"kind": "fill", "label": "Número", "text": "F-1"},
    ]


def test_las_esperas_grabadas_no_se_repiten(tmp_path):
    """Un `wait` fue una decisión sobre una página que ya no existe; repetirlo
    es dormir por costumbre. El asentamiento tras cada acción ya lo cubre, y
    mejor, porque mira la página en vez del reloj."""
    donde = escribir(tmp_path, {"history": [
        {"action": "Esperar", "kind": "wait"},
        {"action": "Guardar", "kind": "click"},
    ]})
    assert [p["label"] for p in script_from(donde)] == ["Guardar"]


def test_un_fichero_que_no_es_un_guion_se_dice(tmp_path):
    donde = escribir(tmp_path, {"cualquier": "cosa"})
    with pytest.raises(SystemExit) as stop:
        script_from(donde)
    assert "unreadable_script" in str(stop.value.code)


def test_repetir_no_gasta_ni_una_llamada_al_modelo(tmp_path):
    navegador = Navegador([pagina("Nueva Compra", "Cancelar"), pagina("Guardar", "Volver")])
    salida = replay(navegador, [{"kind": "click", "label": "Nueva Compra", "text": None},
                                {"kind": "click", "label": "Guardar", "text": None}], buscar)
    assert salida["model_calls"] == 0
    assert salida["replayed"] == 2
    assert navegador.hecho == [("Nueva Compra", None), ("Guardar", None)]


def test_cada_paso_se_resuelve_contra_una_observacion_fresca():
    """Un id de nodo muere en cuanto la página vuelve a renderizar. Repetir por
    etiqueta es lo que hace que un guion sobreviva a un cambio de maquetación."""
    import inspect
    fuente = inspect.getsource(replay)
    assert "browser.observe(screenshot=False)" in fuente
    assert fuente.index("browser.observe") < fuente.index("browser.act")


def test_cuando_el_guion_ya_no_encaja_se_para_y_se_dice_donde(tmp_path):
    navegador = Navegador([pagina("Nueva Compra"), pagina("Otra cosa")])
    with pytest.raises(SystemExit) as stop:
        replay(navegador, [{"kind": "click", "label": "Nueva Compra", "text": None},
                           {"kind": "click", "label": "Guardar", "text": None}], buscar)
    detalle = json.loads(stop.value.code)
    assert detalle["error"] == "script_diverged"
    assert detalle["at_step"] == 2
    # Lo ya hecho se conserva: media corrida informada es más útil que ninguna.
    assert detalle["done"] == [{"step": 1, "kind": "click", "label": "Nueva Compra"}]
    assert detalle["because"]["available"] == ["Otra cosa"]


def test_no_cae_al_modelo_en_silencio(tmp_path):
    """Caer al agente convertiría una deriva del caso en una corrida verde, y
    nadie se enteraría de que el guion ya no describe la aplicación."""
    import inspect
    fuente = inspect.getsource(replay)
    assert "does not fall back to a model on purpose" in fuente
    for prohibido in ("choose(", "import model", "from .model"):
        assert prohibido not in fuente


def test_una_pagina_que_se_movio_no_se_confunde_con_un_guion_viejo():
    """Son dos fallos distintos y llevan a sitios distintos: uno se arregla
    reescribiendo el caso, el otro esperando o cerrando lo que tapa."""
    navegador = Navegador([pagina("Guardar")], falla_en="Guardar")
    with pytest.raises(SystemExit) as stop:
        replay(navegador, [{"kind": "click", "label": "Guardar", "text": None}], buscar)
    detalle = json.loads(stop.value.code)
    assert detalle["error"] == "page_moved"
    assert "banner" in detalle["because"]


def test_un_guion_escrito_en_windows_se_lee(tmp_path):
    """En Windows casi todo lo que escribe un fichero a mano le pone un BOM
    delante, y un guion escrito a mano es el caso normal aquí. Rechazarlo con
    un error que habla de bytes es culpar al fichero de un fallo del lector."""
    destino = tmp_path / "con-bom.json"
    destino.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"history": [{"action": "Guardar", "kind": "click"}]}).encode("utf-8"))
    assert script_from(str(destino)) == [{"kind": "click", "label": "Guardar", "text": None}]


class NavegadorQueNavega(Navegador):
    """El caso normal en micro-frontends: el paso se lleva la página entera."""

    def __init__(self, paginas, navega_en):
        super().__init__(paginas)
        self.navega_en = navega_en

    def act(self, target, page, text=None):
        if self.navega_en == target.get("label"):
            self.hecho.append((target["label"], text))
            raise replay_module.StalePage("Document changed during evaluation")
        self.hecho.append((target["label"], text))


def test_un_paso_que_navega_no_es_un_paso_fallido():
    """Cada módulo del ERP es una carga completa, así que casi cualquier paso
    que cambie de módulo termina con la misma excepción que un fallo real. Se
    distinguen por el hecho: si la dirección cambió, el click llegó."""
    antes, despues = pagina("Compras"), pagina("Nueva Compra")
    antes["url"], despues["url"] = "https://x.test/", "https://x.test/compras"
    navegador = NavegadorQueNavega([antes, despues], navega_en="Compras")
    salida = replay(navegador, [{"kind": "click", "label": "Compras", "text": None}], buscar)
    assert salida["replayed"] == 1
    assert salida["steps"][0]["navigated_to"] == "https://x.test/compras"


def test_si_la_direccion_no_cambio_sigue_siendo_un_fallo():
    """La inferencia se apoya en un hecho observable; sin ese hecho, no se
    inventa el éxito."""
    quieta = pagina("Guardar")
    quieta["url"] = "https://x.test/mismo"
    navegador = NavegadorQueNavega([quieta], navega_en="Guardar")
    with pytest.raises(SystemExit) as stop:
        replay(navegador, [{"kind": "click", "label": "Guardar", "text": None}], buscar)
    assert json.loads(stop.value.code)["error"] == "page_moved"
