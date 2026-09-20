"""Repetir un recorrido sin volver a preguntarle a un modelo.

La primera corrida de un caso es un juicio: alguien —o algo— decide qué pulsar.
La segunda no lo es. El recorrido ya está escrito, y volver a pagar inferencia
para redescubrirlo es pagar dos veces por la misma decisión, con el añadido de
que un modelo puede elegir distinto y convertir una regresión en un misterio.

Lo que se repite son **etiquetas, no identificadores**. Un id de nodo muere en
cuanto la página vuelve a renderizar, así que cada paso se resuelve contra una
observación fresca, igual que lo haría una persona leyendo la pantalla. Eso es
lo que hace que un replay sobreviva a un cambio de maquetación y falle, en
cambio, cuando de verdad cambió lo que el caso probaba — que es cuando debe
fallar.

🚨 **Un replay no llama al modelo, ni siquiera cuando se pierde.** Caer al
agente en silencio convertiría una deriva del caso en una corrida verde, y
nadie se enteraría de que el guion ya no describe la aplicación. Si un paso no
encaja, se para y se dice cuál y qué había en su lugar.

Las contraseñas no entran aquí: la observación nunca ofrece un campo de tipo
`password`, así que no hay forma de que un guion grabado lleve una dentro.
"""

import json
from pathlib import Path

from .browser import StalePage


def script_from(where):
    """El guion: lo grabado por una corrida, o un fichero escrito a mano.

    Se acepta tanto el `summary.json` completo de una corrida como una lista
    pelada de pasos, porque lo primero es lo que existe después de correr algo
    y lo segundo es lo que alguien escribe cuando ya sabe lo que quiere.
    """
    # utf-8-sig y no utf-8: en Windows casi todo lo que escribe un fichero a
    # mano le pone un BOM delante —PowerShell con -Encoding utf8, el Bloc de
    # notas, varios editores— y `json.loads` lo rechaza con un error que habla
    # de bytes y no de lo que pasa. Un guion escrito a mano es el caso normal
    # aqui, no el raro. Sin BOM se lee igual.
    data = json.loads(Path(where).read_text(encoding="utf-8-sig"))
    steps = data.get("history") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        raise SystemExit(json.dumps({
            "error": "unreadable_script",
            "message": f"{where} has no list of steps: expected a run's summary.json "
                       "or a plain list of {action, kind, text}",
        }))
    keep = []
    for step in steps:
        kind = step.get("kind")
        # `wait` fue una decisión del modelo sobre una página que ya no existe;
        # repetirla es dormir por costumbre. El asentamiento tras cada acción ya
        # lo cubre, y mejor, porque mira la página en vez del reloj.
        if kind in (None, "wait"):
            continue
        label = step.get("action") or step.get("label")
        if not label:
            continue
        keep.append({"kind": kind, "label": label, "text": step.get("text")})
    return keep


def replay(browser, steps, find):
    """Ejecuta el guion contra la pestaña abierta, parando en el primer desajuste.

    `find` se recibe en vez de importarse para no atar este módulo a la puerta
    dirigida: las dos resuelven por la etiqueta que produjo la observación, y
    esa es la única regla que importa aquí.
    """
    done = []
    for at, step in enumerate(steps, start=1):
        page = browser.observe(screenshot=False)
        try:
            target = find(page, step["label"], kind=step["kind"])
        except SystemExit as stop:
            # El detalle de por qué no se encontró —deshabilitado, sólo como
            # tecla, o ausente con la lista de lo que sí había— ya lo compone
            # `find`. Aquí sólo se le añade dónde se rompió el guion.
            detail = stop.code
            try:
                detail = json.loads(stop.code)
            except (TypeError, ValueError):
                pass
            raise SystemExit(json.dumps({
                "error": "script_diverged",
                "at_step": at,
                "looked_for": step["label"],
                "done": done,
                "because": detail,
                "message": "the recorded run no longer matches the application; "
                           "this does not fall back to a model on purpose",
            }))
        was_at = page.get("url")
        try:
            browser.act(target, page, text=step.get("text"))
        except StalePage as moved:
            # Un paso que NAVEGA llega aqui igual que uno que fallo: el
            # documento se va mientras se resuelve, y la unica senal es la
            # misma excepcion. En una aplicacion de micro-frontends eso no es
            # el caso raro — cada modulo es una carga completa, asi que casi
            # cualquier paso que cambie de modulo termina asi.
            #
            # Se distinguen por el hecho, no por la excepcion: si la direccion
            # cambio, el click llego y se lo llevo; si sigue igual, no llego.
            # Es una inferencia, no una certeza, y por eso se dice en la
            # salida en vez de dejarla como un exito mudo.
            landed = None
            try:
                landed = browser.observe(screenshot=False).get("url")
            except Exception:
                landed = None
            if landed and landed != was_at:
                done.append({"step": at, "kind": step["kind"], "label": step["label"],
                             "navigated_to": landed})
                continue
            raise SystemExit(json.dumps({
                "error": "page_moved",
                "at_step": at,
                "looked_for": step["label"],
                "done": done,
                "because": str(moved),
            }))
        done.append({"step": at, "kind": step["kind"], "label": step["label"]})
    return {"replayed": len(done), "steps": done, "model_calls": 0}
