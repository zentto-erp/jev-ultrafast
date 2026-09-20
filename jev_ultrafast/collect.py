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


def inspect_page(browser, url, settle=8.0):
    """Lo que la página dice DE SI MISMA: errores, llamadas caídas, controles
    apagados.

    Es la otra mitad del trabajo de un agente de QA. `harvest` responde "qué
    dice la pantalla"; esto responde "¿está sana?". Y responde con hechos que
    la página misma produjo, no con una opinión sobre si la aplicación va bien.

    🚨 La distinción no es un matiz. Una pantalla puede verse perfecta y estar
    montada sobre una respuesta que nunca llegó; y al revés, un defecto real se
    le echa al conductor cuando no hay forma de separar "falló la aplicación"
    de "falló el click". Por eso el veredicto cuenta lo observado y no dice
    nunca que un flujo funcione: esto mira UNA pantalla, no prueba un camino.

    El armado va ANTES de navegar. Después es tarde: los errores de consola y
    los diálogos de la carga —que son la mayoría— ya habrían ocurrido sin nadie
    escuchando.
    """
    browser.arm()
    browser.navigate(url)
    browser.wait_until_settled()
    if settle:
        import time
        time.sleep(settle)

    seen = browser.console(clear=False) or {}
    calls = (browser.network(all_calls=False) or {}).get("calls", [])
    page = browser.observe(screenshot=False)

    console = [one for one in seen.get("entries", [])
               if one.get("level") in ("error", "uncaught", "unhandled")]
    warnings = [one for one in seen.get("entries", []) if one.get("level") == "warn"]
    return {
        "url": page.get("url", url),
        "title": page.get("title"),
        "errors": console[:20],
        "warnings": warnings[:10],
        "failed_calls": calls[:20],
        # Un diálogo nativo que salta solo durante la carga es una pregunta que
        # nadie contestó hasta ahora; saber QUE se pregunto es un hallazgo.
        "dialogs": seen.get("dialogs", [])[:10],
        # Un control apagado no es un defecto, pero la razon de estarlo —cuando
        # la pagina la publica— es lo que separa "hay que rellenar algo" de
        # "esto no deberia estar gris".
        "blocked": page.get("blocked", [])[:12],
        "reachable_controls": len(page.get("actions", [])),
        "keys": page.get("keys", []),
        "verdict": {
            "errors": len(console),
            "failed_calls": len(calls),
            "dialogs": len(seen.get("dialogs", [])),
            # Limpio quiere decir "esta pantalla no se quejo", no "la
            # aplicacion funciona". Decir lo segundo desde aqui seria mentir.
            "clean": not console and not calls,
            "means": "this is one screen reporting on itself, not a flow that was tested",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev-collect", description=__doc__)
    parser.add_argument("urls", nargs="+", help="las páginas a leer")
    parser.add_argument("--match", help="quedarse solo con lo que contenga este texto")
    parser.add_argument("--settle", type=float, default=8.0,
                        help="segundos de margen tras asentarse, para las cifras que llegan tarde")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="el Chrome sin ventana al que hablar")
    parser.add_argument("--out", help="escribir el JSON a un fichero en vez de a la salida")
    parser.add_argument("--inspect", action="store_true",
                        help="en vez de los datos, lo que la pagina dice de si misma: "
                             "errores, llamadas caidas y controles apagados")
    args = parser.parse_args(argv)

    browser = open_browser(args.endpoint)
    try:
        look = inspect_page if args.inspect else None
        pages = [look(browser, url, args.settle) if look
                 else collect(browser, url, args.match, args.settle)
                 for url in args.urls]
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
