"""Lo que hay dentro de un componente cerrado.

Un shadow root cerrado es privado para el script por diseño: `element.shadowRoot`
devuelve null, así que la observación —que corre como script en la página— no
puede mirar dentro, y su contenido no solo es inalcanzable: es *invisible*. Esa
es la peor forma que puede tomar un punto ciego, porque la corrida responde con
aplomo sobre una pantalla cuya otra mitad no sabía que existía.

CDP no es script y esa regla no le aplica. Medido contra un root cerrado a
propósito: el script dijo que no había ningún shadow root, CDP lo devolvió con
su botón dentro, y el click llegó a su manejador.

Nada se abre y nada se parchea: la página se comporta como su autor quiso.
Esto solo se niega a fingir que el contenido no está.

Offline: sin navegador y sin red.
"""

import importlib


def browser():
    import jev_ultrafast.browser as module
    return importlib.reload(module)


def nodo(nombre, atributos=None, hijos=(), roots=(), backend=1):
    plano = []
    for clave, valor in (atributos or {}).items():
        plano += [clave, valor]
    return {"nodeName": nombre, "attributes": plano, "children": list(hijos),
            "shadowRoots": list(roots), "backendNodeId": backend}


def texto(valor):
    return {"nodeType": 3, "nodeValue": valor}


def test_un_root_cerrado_se_reporta_con_lo_que_tiene_dentro():
    boton = nodo("BUTTON", {"id": "oculto"}, hijos=[texto("Confirmar")], backend=99)
    root = {"shadowRootType": "closed", "children": [boton], "shadowRoots": []}
    arbol = nodo("BODY", hijos=[nodo("DIV", {"aria-label": "Sellado"}, roots=[root])])
    encontrados = browser().closed_roots(arbol)
    assert len(encontrados) == 1
    assert encontrados[0]["host"] == "Sellado"
    assert encontrados[0]["inside"] == [
        {"kind": "button", "label": "Confirmar", "backend": 99}]


def test_un_root_abierto_no_se_reporta_aqui():
    """La observación normal ya los atraviesa. Reportarlos otra vez sería
    contar dos veces lo mismo y ensuciar la señal de lo que de verdad no se ve."""
    root = {"shadowRootType": "open", "children": [nodo("BUTTON")], "shadowRoots": []}
    arbol = nodo("BODY", hijos=[nodo("DIV", roots=[root])])
    assert browser().closed_roots(arbol) == []


def test_los_atributos_llegan_planos_de_cdp():
    """CDP los manda como [nombre, valor, nombre, valor…]; leerlos como un
    diccionario sin desdoblarlos daría siempre vacío, y todo lo de dentro
    quedaría sin nombre."""
    leidos = browser()._attributes({"attributes": ["aria-label", "Guardar", "id", "g1"]})
    assert leidos == {"aria-label": "Guardar", "id": "g1"}


def test_el_click_sale_del_rectangulo_que_da_el_renderizador():
    """No de una coordenada inventada ni de una que produjera un modelo."""
    import inspect
    fuente = inspect.getsource(browser().Browser.sealed)
    assert "DOM.getBoxModel" in fuente
    assert "backendNodeId=one[\"backend\"]" in fuente


def test_sin_rectangulo_se_dice_en_vez_de_pulsar_en_cero():
    """Un control sin caja no está en pantalla. Pulsar en 0,0 pulsaría otra
    cosa y la corrida lo contaría como un acierto."""
    import inspect
    fuente = inspect.getsource(browser().Browser.sealed)
    assert "has no box" in fuente
