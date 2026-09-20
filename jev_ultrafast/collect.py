"""Recoger datos de una página y devolverlos como JSON, sin abrir una ventana.

Conducir una aplicación y recoger datos de un sitio son dos trabajos distintos
y se les nota en lo que necesitan. Conducir quiere una ventana que se pueda
mirar, una sesión iniciada y pasos dirigidos. Recoger no quiere ninguna de las
tres: quiere ir, leer lo que la página enseña, y devolverlo estructurado.

    python -m jev_ultrafast.collect https://ejemplo.com/mercados
    python -m jev_ultrafast.collect https://ejemplo.com --match "Bitcoin"
    python -m jev_ultrafast.collect https://a.com https://b.com --settle 8

🚨 Corre en su **propio navegador y su propio perfil**, no en el de QA. Son dos
cosas: el perfil de QA tiene una sesión iniciada contra el ERP, y llevarlo a un
sitio ajeno mezcla un navegador con credenciales dentro con la navegación por
sitios que no controlamos. Aquí no hay sesión que mezclar.

Solo lee. No pulsa, no escribe, no envía formularios y no manda nada a ninguna
parte: lo que devuelve es lo que la página ya le está enseñando a quien la
mira.
"""

import argparse
import json
import os
import sys

from browser_harness.admin import ensure_daemon

from .browser import Browser

# Un daemon y un puerto propios. Si compartiera puerto con el navegador visible,
# el arranque encontraria ESE ya escuchando, devolveria su endpoint, y todo
# seguiria corriendo con ventana sin que nadie se enterase de que el modo sin
# ventana no hizo nada.
DAEMON = "jev-collect"
DEFAULT_ENDPOINT = "http://127.0.0.1:9224"


def open_browser(endpoint):
    """Un navegador contra el Chrome sin ventana, con su propio daemon."""
    env = {"BU_CDP_URL": endpoint}
    os.environ["BU_CDP_URL"] = endpoint
    ensure_daemon(name=DAEMON, env=env)
    # Pestaña propia en vez de reutilizar una por id: un id sacado de /json/list
    # no le sirve al daemon, que resuelve los targets por su propia sesión, y el
    # sintoma es un "No target with given id found" que parece un fallo del
    # navegador y es una confusion de quien pregunta.
    return Browser(None, viewport="none")


def collect(browser, url, match=None, settle=8.0):
    """Ir, dejar que la página termine, y devolver lo que repite."""
    browser.navigate(url)
    # Una página de datos termina de cargar DESPUES de decir que cargó: el
    # armazón llega primero y las cifras después, así que observar en cuanto
    # `readyState` dice `complete` devuelve una lista vacía de una página llena.
    browser.wait_until_settled()
    if settle:
        import time
        time.sleep(settle)
    harvested = browser.harvest(match)
    return harvested or {"url": url, "groups": []}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev-collect", description=__doc__)
    parser.add_argument("urls", nargs="+", help="las páginas a leer")
    parser.add_argument("--match", help="quedarse solo con lo que contenga este texto")
    parser.add_argument("--settle", type=float, default=8.0,
                        help="segundos de margen tras asentarse, para las cifras que llegan tarde")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="el Chrome sin ventana al que hablar")
    parser.add_argument("--out", help="escribir el JSON a un fichero en vez de a la salida")
    args = parser.parse_args(argv)

    browser = open_browser(args.endpoint)
    try:
        pages = [collect(browser, url, args.match, args.settle) for url in args.urls]
    finally:
        browser.close()
    # Una página sola devuelve la página; varias devuelven la lista. Envolver
    # siempre obligaria a desenvolver siempre, y el caso normal es una.
    payload = pages[0] if len(pages) == 1 else {"pages": pages}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        return {"written": args.out, "pages": len(pages)}
    return payload


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, ensure_ascii=False))
    sys.exit(0)
