"""Recoger datos de una página, y la puerta HTTP que lo ofrece.

Un servicio que trae la URL que le pidan es, por construcción, un proxy hacia
la red de quien lo hospeda: bastaría pedirle `http://127.0.0.1:8200` o una IP
interna para que devolviera lo que hay ahí. Los frenos son la mitad del
producto, así que se prueban como tal.

Offline: sin navegador y sin red — la resolución de nombres se sustituye.
"""


import pytest

from jev_ultrafast import serve


def resolving(monkeypatch, address):
    """Un `getaddrinfo` que siempre devuelve esta dirección."""
    monkeypatch.setattr(serve.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (address, 443))])


def test_una_direccion_publica_pasa(monkeypatch):
    resolving(monkeypatch, "93.184.216.34")
    assert serve.reachable("https://example.com/x") is None


@pytest.mark.parametrize("address", [
    "127.0.0.1",        # el propio servicio y todo lo que escuche en local
    "10.1.2.3",         # red interna
    "192.168.1.112",    # el Proxmox de casa
    "172.17.0.1",       # la puerta de Docker
    "169.254.169.254",  # el endpoint de metadatos de las nubes
])
def test_lo_interno_se_rechaza(monkeypatch, address):
    resolving(monkeypatch, address)
    assert serve.reachable("https://parece-publico.com") is not None


def test_el_nombre_se_resuelve_no_se_lee(monkeypatch):
    """Comprobar el TEXTO de la URL no sirve: un dominio público puede apuntar
    a 127.0.0.1, y ese es el truco entero."""
    resolving(monkeypatch, "127.0.0.1")
    why = serve.reachable("https://un-dominio-perfectamente-publico.com")
    assert why and "127.0.0.1" in why


def test_solo_http_y_https():
    for url in ("file:///etc/passwd", "data:text/html,x", "chrome://settings", "ftp://x/y"):
        assert serve.reachable(url) == "only http and https"


def test_una_sola_direccion_mala_basta(monkeypatch):
    """Un nombre puede resolver a varias y la elección no es nuestra."""
    monkeypatch.setattr(serve.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)),
        (2, 1, 6, "", ("127.0.0.1", 443)),
    ])
    assert serve.reachable("https://mitad-y-mitad.com") is not None


def test_sin_token_no_arranca(monkeypatch):
    """Un recolector de URLs sin autenticar es un proxy abierto, así que
    negarse a arrancar es más seguro que arrancar y confiar."""
    monkeypatch.delenv("JEV_COLLECT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stop:
        serve.main(["--port", "0"])
    assert "JEV_COLLECT_TOKEN" in str(stop.value)


def test_escucha_solo_en_local_por_defecto():
    """Exponerlo debe ser una decisión que alguien tome, no el defecto."""
    import inspect
    fuente = inspect.getsource(serve.main)
    assert 'default="127.0.0.1"' in fuente


def test_el_token_se_compara_en_tiempo_constante():
    """Con `==`, el tiempo de respuesta filtra cuántos caracteres son
    correctos."""
    import inspect
    assert "compare_digest" in inspect.getsource(serve.handler_for)


def test_un_navegador_caido_no_se_hereda():
    """Si no se suelta, este objeto sirve errores para siempre y hay que
    reiniciar el servicio a mano."""
    import inspect
    fuente = inspect.getsource(serve.Collector.gather)
    assert "self.browser = None" in fuente
    assert "raise" in fuente


def test_las_peticiones_se_sirven_de_una_en_una():
    """Hay una sesión CDP por navegador y el estado es de la pestaña: servir
    dos a la vez mezcla las dos y devuelve a cada una parte de la otra."""
    import inspect
    assert "self.lock" in inspect.getsource(serve.Collector.gather)


def test_el_log_por_defecto_esta_apagado():
    """Escribe la URL pedida en stderr, y aquí eso es el contenido de la
    petición."""
    import inspect
    assert "def log_message" in inspect.getsource(serve.handler_for)


def test_inspeccionar_arma_antes_de_navegar():
    """Después es tarde: los errores de consola y los diálogos de la carga
    —que son la mayoría— ya habrían ocurrido sin nadie escuchando."""
    import inspect as reflect

    from jev_ultrafast.collect import inspect_page
    fuente = reflect.getsource(inspect_page)
    assert fuente.index("browser.arm()") < fuente.index("browser.navigate(")


def test_el_veredicto_no_dice_que_la_aplicacion_funcione():
    """Esto mira UNA pantalla. Decir desde aquí que un flujo va bien sería
    exactamente el fallo que este motor existe para no cometer."""
    import inspect as reflect

    from jev_ultrafast.collect import inspect_page
    fuente = reflect.getsource(inspect_page)
    assert "not a flow that was tested" in fuente
    for prohibido in ('"pass"', "'PASS'", '"ok": True'):
        assert prohibido not in fuente


def test_limpio_exige_las_dos_cosas():
    """Una pantalla sin errores de consola pero con un 500 detrás no está
    limpia: se ve bien y está montada sobre una respuesta que no llegó."""
    import inspect as reflect

    from jev_ultrafast.collect import inspect_page
    assert '"clean": not console and not ours' in reflect.getsource(inspect_page)


def test_las_dos_puertas_van_al_mismo_navegador():
    """Separarlas evita que una corrida que solo quería datos cargue con un
    informe de errores, y al revés."""
    import inspect as reflect
    fuente = reflect.getsource(serve.handler_for)
    assert 'self.path not in ("/collect", "/inspect")' in fuente
    assert 'look=self.path == "/inspect"' in fuente


def test_el_veredicto_no_lo_deciden_los_rastreadores_ajenos():
    """Medido contra un sitio real: diez llamadas caídas y NUEVE eran
    rastreadores de terceros que el propio navegador bloqueó. Contando
    aquello, `clean` sale falso en cualquier sitio de internet, y un informe
    que siempre dice que hay un problema es tan inútil como uno que nunca lo
    dice. Se listan igual, pero aparte."""
    import inspect as reflect

    from jev_ultrafast.collect import inspect_page
    fuente = reflect.getsource(inspect_page)
    assert 'one.get("third_party")' in fuente
    assert '"failed_third_party": theirs' in fuente
    assert '"clean": not console and not ours' in fuente


def test_el_host_se_conserva_cuando_la_llamada_es_de_otro():
    """Recortando siempre a la ruta se pierde de quién era, y sin eso no se
    puede separar un 500 de la aplicación de un rastreador bloqueado."""
    from pathlib import Path
    fuente = (Path(serve.__file__).parent / "browser.py").read_text(encoding="utf-8")
    assert "third = u.origin !== location.origin" in fuente
    assert "u.host + u.pathname + u.search" in fuente


def test_un_reinicio_de_chrome_no_se_convierte_en_un_502(monkeypatch):
    """Chrome se reinicia —se cae, lo reinicia systemd, se actualiza— y la
    conexión muere con él. Al que llama eso le llegaba como un 502, que le dice
    que su petición estaba mal cuando lo único que pasó es que el navegador se
    estaba levantando."""
    intentos = {"abiertos": 0}

    class Caido:
        def __init__(self, falla):
            self.falla = falla

        def close(self):
            pass

    def abrir(endpoint):
        intentos["abiertos"] += 1
        return Caido(falla=intentos["abiertos"] == 1)

    def recoger(browser, url, match, settle):
        if browser.falla:
            raise RuntimeError("WebSocket connection closed")
        return {"url": url, "groups": []}

    monkeypatch.setattr(serve, "open_browser", abrir)
    monkeypatch.setattr(serve, "collect", recoger)
    salida = serve.Collector("http://x").gather(["https://ejemplo.com"], None, 0)
    assert salida == [{"url": "https://ejemplo.com", "groups": []}]
    assert intentos["abiertos"] == 2, "no abrió un navegador nuevo para el segundo intento"


def test_el_segundo_fallo_si_se_reporta(monkeypatch):
    """Reintentar para siempre esconderia un navegador que no arranca."""
    monkeypatch.setattr(serve, "open_browser", lambda endpoint: type("X", (), {"close": lambda s: None})())
    monkeypatch.setattr(serve, "collect", lambda *a: (_ for _ in ()).throw(RuntimeError("muerto")))
    with pytest.raises(RuntimeError, match="muerto"):
        serve.Collector("http://x").gather(["https://ejemplo.com"], None, 0)


def test_reintentar_solo_vale_porque_esto_solo_lee():
    """Una accion que cambia algo no se reintenta nunca: no hay forma de saber
    si el primer intento llego, y repetirlo duplicaria lo que hizo."""
    import inspect

    assert "SOLO LEE" in inspect.getsource(serve.Collector.gather)
