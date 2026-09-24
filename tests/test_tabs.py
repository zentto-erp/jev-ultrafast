"""Contratos de las pestanas: un sistema web no cabe en una.

Sin navegador y sin APIs de pago: CDP se sustituye por un grabador, asi que lo
que se fija aqui son las decisiones y no el comportamiento de Chrome.

Lo que se prueba, y por que cada cosa:

- Una pestana que se abre no se cierra sola, y una prestada no se cierra nunca.
  Con una sola bandera de propiedad eso no se podia distinguir en cuanto habia
  dos pestanas abiertas.
- Las demas pestanas llegan a la observacion como operaciones. Si no llegan, el
  agente no puede elegirlas: en el bucle solo existe lo que la observacion
  ofrece, y el resultado es que concluye que el boton no hizo nada cuando en
  realidad abrio el PDF al lado.
- Aparecer una pestana no invalida la decision en curso. Tratarlo como cambio de
  pagina dejaria en bucle a cualquier aplicacion que abra una ventana al cargar.
"""

import pytest

from jev_ultrafast import browser as browser_mod
from jev_ultrafast.browser import Browser


class FakeCdp:
    """Graba las llamadas CDP y contesta las que hacen falta, incluidas las pestanas."""

    def __init__(self, targets=None):
        self.calls = []
        self.created = []
        self.closed = []
        self.attached = []
        self.targets = list(targets or [])
        self._next = 0

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "Target.createTarget":
            self._next += 1
            target = f"opened-{self._next}"
            self.created.append(target)
            self.targets.append({"targetId": target, "type": "page",
                                 "url": params.get("url", ""), "title": ""})
            return {"targetId": target}
        if method == "Target.attachToTarget":
            self.attached.append(params.get("targetId"))
            return {"sessionId": "session-" + str(params.get("targetId"))}
        if method == "Target.closeTarget":
            gone = params.get("targetId")
            self.closed.append(gone)
            self.targets = [t for t in self.targets if t["targetId"] != gone]
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if "readyState" in expression:
                return {"result": {"value": "complete"}}
            if "innerHTML.length" in expression:
                return {"result": {"value": 100}}
        return {}


@pytest.fixture
def fake(monkeypatch):
    recorder = FakeCdp()
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)
    return recorder


def test_cierra_la_que_abrio_y_deja_la_prestada(fake):
    """La propiedad es por pestana. Con una bandera unica esto no se podia decidir."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    browser.open_tab("https://dos.test/")
    assert browser.target == "opened-1"
    # Se conduce una que es nuestra, pero la prestada sigue sin serlo.
    assert browser.owns_target is True

    browser.close()
    assert fake.closed == ["opened-1"], "la prestada no se cierra, la abierta si"


def test_cambiar_de_pestana_no_cierra_la_que_se_deja(fake):
    """Volver tiene que seguir siendo posible, y puede ser la pestana del usuario."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.append({"targetId": "prestada", "type": "page",
                         "url": "https://uno.test/", "title": "Uno"})
    browser.open_tab("https://dos.test/")
    browser.switch("prestada")

    assert browser.target == "prestada"
    assert fake.closed == []
    assert "prestada" in fake.attached


def test_no_se_puede_conducir_una_pestana_que_no_existe(fake):
    """Un id inventado tiene que fallar aqui y no dos pasos despues."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    with pytest.raises(ValueError, match="pestana"):
        browser.switch("no-existe")


def test_la_cli_abre_una_pestana_que_le_sobrevive(fake):
    """Un proceso por paso no puede ser dueno de algo que debe durar mas que el."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    browser.open_tab("https://dos.test/", own=False)
    browser.close()
    assert fake.closed == [], "sin adoptarla, el cierre del comando no se la lleva"


def test_las_demas_pestanas_llegan_a_la_observacion_como_operaciones(fake):
    """En el bucle solo existe lo que la observacion ofrece."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.extend([
        {"targetId": "prestada", "type": "page", "url": "https://uno.test/", "title": "Uno"},
        {"targetId": "otra", "type": "page", "url": "https://dos.test/factura.pdf",
         "title": "Factura 1042"},
    ])
    page = {"actions": [{"id": "1", "kind": "click", "label": "Imprimir"}]}
    browser.offer_other_tabs(page)

    ofrecidas = [a for a in page["actions"] if a["kind"] == "tab"]
    assert len(ofrecidas) == 1
    assert ofrecidas[0]["target"] == "otra"
    assert "Factura 1042" in ofrecidas[0]["label"]
    assert ofrecidas[0]["id"] == "TAB_1"
    # El control de la pantalla sigue ahi: las pestanas se suman, no sustituyen.
    assert any(a["id"] == "1" for a in page["actions"])


def test_con_una_sola_pestana_no_se_ofrece_nada(fake):
    """Ofrecer 'ir a la pestana actual' seria una opcion que nunca es correcta."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.append({"targetId": "prestada", "type": "page",
                         "url": "https://uno.test/", "title": "Uno"})
    page = {"actions": []}
    browser.offer_other_tabs(page)
    assert page["actions"] == []
    assert page["tabs"] == []


def test_las_pantallas_del_navegador_no_se_ofrecen(fake):
    """Existen, pero no son pantallas de la aplicacion."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.extend([
        {"targetId": "prestada", "type": "page", "url": "https://uno.test/", "title": "Uno"},
        {"targetId": "devtools", "type": "page", "url": "devtools://devtools/x", "title": "DevTools"},
        {"targetId": "ajustes", "type": "page", "url": "chrome://settings", "title": "Settings"},
        {"targetId": "worker", "type": "service_worker", "url": "https://uno.test/sw.js", "title": ""},
    ])
    page = {"actions": []}
    browser.offer_other_tabs(page)
    assert page["actions"] == []


def test_si_el_navegador_no_contesta_la_observacion_sigue_siendo_buena(fake, monkeypatch):
    """Saber que otras pestanas hay es una ayuda, no un requisito del paso."""
    browser = Browser("https://uno.test/", reuse_target="prestada")

    def rota():
        raise RuntimeError("el daemon no contesta")

    monkeypatch.setattr(browser, "tabs", rota)
    page = {"actions": [{"id": "1", "kind": "click", "label": "Guardar"}]}
    assert browser.offer_other_tabs(page)["actions"][0]["id"] == "1"


def test_cerrar_la_pestana_que_se_conduce_pasa_a_otra(fake):
    """Conducir una pestana cerrada falla con un error que habla de sesiones."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.append({"targetId": "prestada", "type": "page",
                         "url": "https://uno.test/", "title": "Uno"})
    browser.open_tab("https://dos.test/")
    conducida = browser.target

    result = browser.close_tab()
    assert result["closed"] == conducida
    assert browser.target == "prestada"


def test_cerrar_la_ultima_deja_sin_pestana_y_lo_dice(fake):
    """Un estado sin pestana es legitimo al terminar; lo que no vale es ocultarlo."""
    browser = Browser("https://uno.test/", reuse_target="prestada")
    fake.targets.append({"targetId": "prestada", "type": "page",
                         "url": "https://uno.test/", "title": "Uno"})
    result = browser.close_tab("prestada")
    assert result["driving"] is None
    assert result["tabs"] == []


def test_las_pestanas_que_ya_estaban_no_se_ofrecen(monkeypatch):
    """Medido contra el Chrome real de pruebas: habia quince de sesiones viejas.

    Ofrecerlas no es una capacidad, son quince opciones de ruido en el espacio
    de decision, todas incorrectas, compitiendo con los controles de la pantalla.
    El agente no necesita saber que pestanas existen: necesita saber cual acaba
    de aparecer.
    """
    recorder = FakeCdp(targets=[
        {"targetId": "vieja-x", "type": "page", "url": "https://x.com/", "title": "Home / X"},
        {"targetId": "vieja-fb", "type": "page", "url": "https://facebook.com/", "title": "Facebook"},
        {"targetId": "prestada", "type": "page", "url": "https://erp.test/", "title": "ERP"},
    ])
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)

    browser = Browser(None, reuse_target="prestada")
    page = {"actions": []}
    browser.offer_other_tabs(page)
    assert page["actions"] == [], "dos pestanas del usuario, ninguna ofrecida"

    # Ahora la aplicacion abre el PDF de la factura por su cuenta.
    recorder.targets.append({"targetId": "pdf-factura", "type": "page",
                             "url": "https://erp.test/factura/1042.pdf", "title": "Factura 1042"})
    page = {"actions": []}
    browser.offer_other_tabs(page)
    ofrecidas = [a for a in page["actions"] if a["kind"] == "tab"]
    assert len(ofrecidas) == 1, "solo la que aparecio durante el recorrido"
    assert ofrecidas[0]["target"] == "pdf-factura"
    assert "durante el recorrido" in ofrecidas[0]["label"]


def test_sin_foto_inicial_solo_se_ofrecen_las_que_abrio_el_recorrido(monkeypatch):
    """Si el navegador no contesta al arrancar, se degrada; no se cae ni inventa."""

    class SinTargets(FakeCdp):
        def __call__(self, method, **params):
            if method == "Target.getTargets" and not self.calls:
                self.calls.append((method, params))
                return {}
            return super().__call__(method, **params)

    recorder = SinTargets(targets=[
        {"targetId": "vieja", "type": "page", "url": "https://x.com/", "title": "X"},
        {"targetId": "prestada", "type": "page", "url": "https://erp.test/", "title": "ERP"},
    ])
    monkeypatch.setattr(browser_mod, "cdp", recorder)
    monkeypatch.setattr(browser_mod, "ensure_daemon", lambda: None)
    monkeypatch.setattr(browser_mod.time, "sleep", lambda _s: None)

    browser = Browser(None, reuse_target="prestada")
    # `None`, no un conjunto vacio: "no lo se" y "no habia ninguna" son cosas
    # distintas, y confundirlas es lo que colaba las pestanas del usuario.
    assert browser.tabs_before is None
    page = {"actions": []}
    browser.offer_other_tabs(page)
    # Sin foto inicial todo "aparecio", asi que la vieja se colaria. Eso es lo
    # que NO debe pasar: se cae al criterio seguro, las que abrimos nosotros.
    ofrecidas = [a for a in page["actions"] if a["kind"] == "tab"]
    assert [a["label"] for a in ofrecidas] == [], "sin foto inicial no se adivina"


def test_la_propiedad_sigue_admitiendo_que_se_le_asigne(fake):
    """Era un atributo normal y habia codigo que lo ESCRIBIA.

    Un puente que conduce el navegador de otro —el que graba un video, por
    ejemplo— le dice "esta pestana no es tuya" para que no la cierre al
    terminar. Al convertirlo en propiedad calculada eso reventaba con "property
    has no setter", y el fallo aparecia lejos de su causa: el recorrido no hacia
    nada y el unico rastro era un resumen vacio.

    Una propiedad que sustituye a un atributo tiene que admitir lo que el
    atributo admitia, o no es un detalle interno: es un cambio de contrato.
    """
    browser = Browser("https://uno.test/")          # abre la suya, luego es suya
    assert browser.owns_target is True

    browser.owns_target = False                      # el puente la suelta
    assert browser.owns_target is False
    browser.close()
    assert fake.closed == [], "soltada, el cierre no se la lleva"

    browser.owns_target = True                       # y se puede volver a adoptar
    assert browser.owns_target is True
