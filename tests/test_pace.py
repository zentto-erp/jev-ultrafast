"""Contratos del ritmo: el mismo recorrido, a la velocidad que pide su proposito.

Cuando lo que se quiere es un resultado —probar una pantalla, rellenar un
formulario, sacar datos— el ritmo ideal es ninguno. Cuando lo que se quiere es un
video, ese mismo recorrido no sirve: un clic que ocurre en el mismo fotograma en
que el cursor aparece no se ve, y el tutorial no ensena nada aunque cada paso
funcione.

Es un ajuste y no dos motores a proposito. Un recorrido grabado tiene que
ejercitar exactamente el mismo camino que el que se prueba, o el video ensena
algo que la aplicacion no hace.
"""

from unittest.mock import Mock

import pytest

from jev_ultrafast.browser import approach, pace


@pytest.fixture(autouse=True)
def sin_ritmo_heredado(monkeypatch):
    """Ninguna prueba debe depender de lo que el entorno traiga puesto."""
    monkeypatch.delenv("JEV_PACE", raising=False)


def test_por_defecto_no_hay_ritmo():
    """Lo de siempre: quien no pide nada sigue corriendo igual de rapido."""
    r = pace()
    assert r["modo"] == "fast"
    assert r["aproximar"] == 0
    assert r["antes_del_clic"] == 0.0
    assert r["despues_del_clic"] == 0.0
    assert r["por_tecla_ms"] == 0


@pytest.mark.parametrize("valor", ["human", "humano", "video", "HUMAN", " video "])
def test_el_ritmo_humano_se_pide_por_su_nombre(monkeypatch, valor):
    """Varios nombres para lo mismo: quien graba un video no piensa en 'human'."""
    monkeypatch.setenv("JEV_PACE", valor)
    r = pace()
    assert r["modo"] == "human"
    assert r["aproximar"] > 0
    assert r["por_tecla_ms"] > 0


def test_un_valor_desconocido_no_ralentiza_nada(monkeypatch):
    """Un typo no puede convertir un barrido de cuatrocientas pantallas en una
    tarde: ante la duda, el ritmo de siempre."""
    monkeypatch.setenv("JEV_PACE", "lentito")
    assert pace()["modo"] == "fast"


def test_sin_ritmo_el_cursor_no_se_mueve():
    """Un movimiento que nadie va a ver es tiempo regalado."""
    call = Mock()
    approach(call, 100, 200, pace())
    assert call.call_count == 0


def test_con_ritmo_el_cursor_recorre_hasta_el_control(monkeypatch):
    """Un solo evento en el destino es indistinguible de no haberse movido.

    Y no es decoracion: el movimiento dispara los `hover` de la pagina, y hay
    menus que solo se despliegan al pasar por encima. Un clic teletransportado se
    salta ese estado intermedio.
    """
    monkeypatch.setenv("JEV_PACE", "human")
    call = Mock()
    r = pace()
    r["antes_del_clic"] = 0  # el contrato es el recorrido, no la espera
    approach(call, 400, 300, r)

    movimientos = [c for c in call.call_args_list if c.args[0] == "Input.dispatchMouseEvent"]
    assert len(movimientos) == r["aproximar"] > 1
    assert all(c.kwargs["type"] == "mouseMoved" for c in movimientos)
    # Acaba exactamente encima del control, no cerca.
    assert movimientos[-1].kwargs["x"] == 400
    assert movimientos[-1].kwargs["y"] == 300
    # Y viene de otro sitio: el primero no puede estar ya en el destino.
    assert movimientos[0].kwargs["x"] != 400
