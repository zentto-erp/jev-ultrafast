"""Contratos de la cache de decisiones: repetir sin volver a pagar, y sin mentir.

Sin navegador y sin APIs de pago. Lo que se fija aqui es lo que distingue una
cache util de una peligrosa:

- La clave es el objetivo y el paso, no la pantalla. Con la pantalla dentro, un
  cambio de etiqueta produce un fallo de cache en vez de una divergencia, y
  entonces el modo estricto nunca dispara y toda deriva sale en verde. Tres
  pruebas de este fichero, escritas sobre la version anterior, fueron las que lo
  destaparon.
- Lo guardado son etiquetas, no identificadores. Un id muere en el siguiente
  render.
- El modo estricto NO cae al modelo. Que el guion deje de encajar es el resultado
  de la prueba; curarlo en silencio convierte una deriva de la aplicacion en una
  corrida verde y nadie se entera.
- Una decision repetida no finge haberse juzgado: su probabilidad es `None`, no
  1.0. Un numero inventado ahi hace inauditable el registro de la corrida.
"""

import json

import pytest

from jev_ultrafast.agent import Agent
from jev_ultrafast.decisions import Decisions, Diverged, key, resolve


def page(actions=None, text="Total: 10 articulos", url="https://erp.test/facturas"):
    return {
        "url": url,
        "title": "Facturas",
        "text": text,
        "actions": actions if actions is not None else [
            {"id": "e1", "kind": "click", "label": "Nueva factura", "node": 1},
            {"id": "e2", "kind": "fill", "label": "Buscar", "node": 2},
        ],
    }


def test_el_mismo_paso_del_mismo_objetivo_es_la_misma_decision():
    assert key("crear factura", 3) == key("crear factura", 3)


def test_otro_objetivo_u_otro_paso_son_otra_decision():
    assert key("crear factura", 3) != key("anular factura", 3)
    assert key("crear factura", 3) != key("crear factura", 4)


def test_la_direccion_se_comprueba_al_resolver_no_en_la_clave():
    """El paso 3 estaba en facturas; si ahora estamos en otra pantalla no encaja,
    y da igual que exista un boton con el mismo nombre.

    Al resolver y no en la clave porque ahi una discrepancia SIGNIFICA algo: es una
    divergencia que hay que reportar, no una entrada que no existe.
    """
    guardado = {"operation": "CLICK", "kind": "click", "label": "Nueva factura",
                "url": "https://erp.test/facturas"}
    assert resolve(page(), guardado)["id"] == "e1"
    assert resolve(page(url="https://erp.test/notas"), guardado) is None


def test_el_texto_de_la_pantalla_no_puede_invalidar_nada():
    """Un reloj o un contador de notificaciones no cambian que hay que pulsar."""
    guardado = {"operation": "CLICK", "kind": "click", "label": "Nueva factura"}
    assert resolve(page(text="Total: 10"), guardado)["id"] == "e1"
    assert resolve(page(text="Total: 11"), guardado)["id"] == "e1"


def test_se_resuelve_por_etiqueta_no_por_identificador():
    """El id cambia en el siguiente render; la etiqueta es lo que leeria una persona."""
    despues = page(actions=[{"id": "e77", "kind": "click", "label": "Nueva factura", "node": 9}])
    found = resolve(despues, {"operation": "CLICK", "kind": "click", "label": "Nueva factura"})
    assert found["id"] == "e77"


def test_lo_que_ya_no_esta_no_se_resuelve():
    found = resolve(page(), {"operation": "CLICK", "kind": "click", "label": "Anular"})
    assert found is None


def test_terminar_tambien_es_una_decision_que_se_recuerda():
    """Y la mas repetida: casi todo recorrido acaba en DONE."""
    assert resolve(page(), {"operation": "DONE"}) == "DONE"
    assert resolve(page(), {"operation": "BLOCKED"}) == "BLOCKED"


def test_una_pestana_se_recuerda_por_intencion_no_por_titulo():
    """Su titulo cambia con lo que carga, asi que el nombre no sirve de ancla."""
    con_pestana = page(actions=[
        {"id": "e1", "kind": "click", "label": "Imprimir", "node": 1},
        {"id": "TAB_1", "kind": "tab", "label": "Ir a la pestana abierta: Factura 99", "target": "x"},
    ])
    found = resolve(con_pestana, {"operation": "TAB_1", "kind": "tab", "label": None})
    assert found["id"] == "TAB_1"


def test_lo_guardado_sobrevive_al_proceso(tmp_path):
    guardadas = Decisions(tmp_path)
    guardadas.remember("crear", 0, {"operation": "CLICK"},
                       {"kind": "click", "label": "Nueva factura"})
    de_nuevo = Decisions(tmp_path)
    assert de_nuevo.look_up("crear", 0)["label"] == "Nueva factura"
    # Legible con un editor a proposito: una cache que no se puede abrir es una
    # cache en la que no se puede confiar.
    assert json.loads((tmp_path / "decisions.json").read_text(encoding="utf-8"))


def test_un_fichero_con_bom_se_lee_igual(tmp_path):
    """En Windows casi todo lo que escribe un fichero a mano le pone un BOM."""
    (tmp_path / "decisions.json").write_text('{"abc": {"operation": "DONE"}}', encoding="utf-8-sig")
    assert Decisions(tmp_path).entries == {"abc": {"operation": "DONE"}}


def test_un_fichero_ilegible_no_tumba_el_recorrido(tmp_path):
    """Una cache corrupta se ignora: el recorrido puede decidir por su cuenta."""
    (tmp_path / "decisions.json").write_text("no es json", encoding="utf-8")
    assert Decisions(tmp_path).entries == {}


def test_el_modo_estricto_no_cae_al_modelo(tmp_path, monkeypatch):
    """Que el guion deje de encajar ES el resultado de la prueba."""
    from jev_ultrafast import agent as loop

    runner = Agent.__new__(Agent)
    runner.decisions = Decisions(tmp_path, heal=False)
    runner.decisions.remember("crear", 0, {"operation": "CLICK"},
                              {"kind": "click", "label": "Nueva factura"})

    def nadie_deberia_preguntar(*_args, **_kwargs):
        raise AssertionError("el modo estricto no puede llamar al modelo")

    monkeypatch.setattr(loop, "choose", nadie_deberia_preguntar)
    cambiada = page(actions=[{"id": "e1", "kind": "click", "label": "Crear documento", "node": 1}])
    with pytest.raises(Diverged) as parada:
        runner.recall_or_choose(cambiada, "crear", [])
    # Con la lista de lo que si habia: "no encontrado" a secas obliga a repetir la
    # corrida a mano para averiguarlo.
    assert parada.value.offered == [{"kind": "click", "label": "Crear documento"}]
    assert "Nueva factura" in str(parada.value)


def test_el_modo_self_heal_vuelve_a_preguntar_y_lo_apunta(tmp_path, monkeypatch):
    """Un rediseno menor no deberia parar una automatizacion."""
    from jev_ultrafast import agent as loop

    runner = Agent.__new__(Agent)
    runner.decisions = Decisions(tmp_path, heal=True)
    runner.decisions.remember("crear", 0, {"operation": "CLICK"},
                              {"kind": "click", "label": "Nueva factura"})

    nueva = {"choice": "e1", "operation": "CLICK", "confidence": 0.9,
             "probabilities": {"e1": 0.9}, "latency_ms": 12, "usage": {}}
    monkeypatch.setattr(loop, "choose", lambda *_a, **_k: nueva)
    cambiada = page(actions=[{"id": "e1", "kind": "click", "label": "Crear documento", "node": 1}])

    devuelta = runner.recall_or_choose(cambiada, "crear", [])
    assert devuelta["choice"] == "e1"
    assert runner.decisions.healed == 1
    # Y lo nuevo queda guardado, no lo viejo.
    assert runner.decisions.look_up("crear", 0)["label"] == "Crear documento"


def test_una_decision_repetida_no_finge_haberse_juzgado(tmp_path):
    """Poner 1.0 seria comodo y falso: no hubo juicio, hubo una entrada de cache."""
    runner = Agent.__new__(Agent)
    runner.decisions = Decisions(tmp_path)
    runner.decisions.remember("crear", 0, {"operation": "CLICK"},
                              {"kind": "click", "label": "Nueva factura"})

    repetida = runner.recall_or_choose(page(), "crear", [])
    assert repetida["from_cache"] is True
    assert repetida["confidence"] is None
    assert repetida["probabilities"] == {"e1": None}
    assert repetida["latency_ms"] == 0
    assert repetida["model"] == "recordado"
    assert runner.decisions.hits == 1


def test_sin_directorio_no_hay_cache_y_todo_sigue_igual(monkeypatch):
    """Repetir sin haber guardado nada no es mas rapido, es adivinar."""
    from jev_ultrafast import agent as loop

    runner = Agent.__new__(Agent)
    llamadas = []
    monkeypatch.setattr(loop, "choose", lambda *a, **k: llamadas.append(a) or {"choice": "e1"})
    runner.recall_or_choose(page(), "crear", [])
    assert len(llamadas) == 1
