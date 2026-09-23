"""Cuanto pesa una decision, medido sobre la pantalla real y sin pagar por ella.

En TypeSafe se cobra la entrada. La consecuencia es que el gasto de un recorrido
lo fija el tamano de la pantalla y no la dificultad de la tarea: una tabla de
cien filas viaja entera en cada paso, y otra vez en la corrida siguiente. Eso se
sabe desde una sesion que dio 365.710 tokens de entrada en 241 peticiones, pero
no se sabia DONDE — y sin saber donde, cualquier poda es una apuesta.

Este banco responde eso. Observa una pantalla, construye el cuerpo exacto que se
mandaria —el de `request_body`, no una copia— y lo desglosa por seccion. No llama
al modelo, asi que medir no cuesta nada y se puede repetir tantas veces como haga
falta.

Dos cosas que lo hacen util en vez de anecdotico:

- **Guarda la observacion** (`--save`). Con el `page.json` en disco se vuelve a
  medir sin navegador y sin variabilidad: la misma pantalla, byte a byte, antes y
  despues de un cambio. Sin eso, dos medidas de la misma pagina difieren porque
  la pagina difiere, y la mejora que se le atribuye al codigo puede ser del
  reloj.
- **Mide bytes, no tokens estimados.** Los bytes son un hecho; los tokens los
  dice el proveedor. Con `--live` se hace UNA peticion real y se apunta cuantos
  tokens cobro por esos bytes, que es la unica forma honesta de traducir.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_ultrafast.model import request_body  # noqa: E402


def weigh(value):
    """Bytes del JSON de un trozo, tal como viajaria: sin espacios de adorno."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def breakdown(body):
    """El cuerpo abierto por secciones, cada una con lo que pesa.

    Las preguntas se listan una por una a proposito. `operation` y cada
    `*_target` repiten la etiqueta de los mismos elementos, y saber cual de las
    dos cosas pesa decide que se poda: la tabla de elementos o las cabezas de
    target.
    """
    state, questions = body["state"], body["questions"]
    parts = {
        "state.page.url": state["page"].get("url", ""),
        "state.page.title": state["page"].get("title", ""),
        "state.page.text": state["page"].get("text", ""),
        "state.elements": state["elements"],
        "state.recent_actions": state["recent_actions"],
    }
    for name, question in questions.items():
        parts["questions." + name] = question
    total = weigh(body)
    rows = sorted(((name, weigh(part)) for name, part in parts.items()), key=lambda r: -r[1])
    return total, rows


def counts(page, body):
    """Lo que hay en la pantalla, para poder leer los bytes como consecuencia."""
    elements = body["state"]["elements"]
    targets = {
        name: len(question["criteria"])
        for name, question in body["questions"].items()
        if name.endswith("_target")
    }
    return {
        "actions_observed": len(page.get("actions", [])),
        "actions_omitted": page.get("omitted_actions", 0),
        "elements_offered": len(elements),
        "select_options": sum(len(e.get("options", [])) for e in elements),
        "page_text_chars": len(page.get("text", "")),
        "blocked_reported": len(page.get("blocked", [])),
        "targets_per_operation": targets,
    }


def measure(page, goal, history=None):
    body, _operations, _targets, _controls = request_body(page, goal, history or [])
    total, rows = breakdown(body)
    return {
        "url": page.get("url"),
        "title": page.get("title"),
        "goal": goal,
        "total_bytes": total,
        "sections": [
            {"section": name, "bytes": size, "share": round(size / total, 4)} for name, size in rows
        ],
        "counts": counts(page, body),
    }


def observe_url(url, viewport, reuse_tab):
    """Abre la pantalla y devuelve la observacion cruda.

    Se importa aqui y no arriba porque abrir el navegador es lo unico caro de
    este fichero: medir desde un `page.json` guardado no debe arrastrar el
    arranque de Chrome ni fallar si no hay.
    """
    from jev_ultrafast.browser import Browser

    browser = Browser(url, reuse_target=reuse_tab, viewport=viewport)
    try:
        return browser.observe(screenshot=False)
    finally:
        if reuse_tab is None:
            browser.close()


def live(page, goal):
    """Una sola peticion real, para saber que cobro el proveedor por esos bytes."""
    from jev_ultrafast.model import choose

    decision = choose(page, goal, [])
    return {
        "model": decision.get("model"),
        "latency_ms": decision.get("latency_ms"),
        "usage": decision.get("usage", {}),
        "operation": decision.get("operation"),
        "target": decision.get("target"),
    }


def render(results, live_results):
    lines = []
    for result in results:
        lines.append("## " + (result["title"] or "(sin titulo)"))
        lines.append(result["url"] or "")
        lines.append("")
        lines.append("**{:,} bytes** de entrada por paso. Reparto:".format(result["total_bytes"]))
        lines.append("")
        lines.append("| Seccion | Bytes | % |")
        lines.append("|---|---:|---:|")
        for section in result["sections"]:
            if section["bytes"] == 0:
                continue
            lines.append("| {} | {:,} | {:.1f} |".format(
                section["section"], section["bytes"], section["share"] * 100))
        counted = result["counts"]
        lines.append("")
        lines.append(
            "Observado: {} acciones ({} descartadas por tope), {} elementos ofrecidos, "
            "{} opciones de desplegable, {} caracteres de texto, {} controles "
            "deshabilitados reportados.".format(
                counted["actions_observed"], counted["actions_omitted"],
                counted["elements_offered"], counted["select_options"],
                counted["page_text_chars"], counted["blocked_reported"])
        )
        lines.append("Targets por operacion: {}".format(counted["targets_per_operation"]))
        lines.append("")
    for measured, answered in zip(results, live_results):
        if not answered:
            continue
        usage = answered["usage"]
        tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        lines.append("### Peticion real — " + (measured["url"] or ""))
        lines.append("{} respondio {} en {} ms. Uso: {}".format(
            answered["model"], answered["operation"], answered["latency_ms"],
            json.dumps(usage, ensure_ascii=False)))
        if tokens:
            lines.append("**{:.2f} bytes por token** de entrada, medido aqui.".format(
                measured["total_bytes"] / tokens))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="measure-payload", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", action="append", default=[], help="pantalla a medir; repetible")
    parser.add_argument("--from", dest="saved", action="append", default=[],
                        help="page.json de una observacion guardada; mide sin navegador")
    parser.add_argument("--goal", default="Revisar la pantalla y reportar lo que no cuadre",
                        help="el goal cambia el cuerpo, asi que forma parte de la medida")
    parser.add_argument("--reuse-tab", help="targetId de una pestana abierta, para no abrir otra")
    parser.add_argument("--viewport", default="fixed")
    parser.add_argument("--save", help="directorio donde dejar cada page.json para re-medir sin navegador")
    parser.add_argument("--live", action="store_true", help="una peticion real, para anclar bytes a tokens")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if not args.url and not args.saved:
        parser.error("hace falta al menos --url o --from")

    pages = []
    for path in args.saved:
        pages.append((path, json.loads(Path(path).read_text(encoding="utf-8-sig"))))
    for url in args.url:
        page = observe_url(url, args.viewport, args.reuse_tab)
        pages.append((url, page))
        if args.save:
            where = Path(args.save)
            where.mkdir(parents=True, exist_ok=True)
            name = "".join(c if c.isalnum() else "-" for c in url)[:80] or "page"
            (where / (name + ".json")).write_text(
                json.dumps(page, ensure_ascii=False), encoding="utf-8")

    results = [measure(page, args.goal) for _source, page in pages]
    live_results = [live(page, args.goal) if args.live else None for _source, page in pages]

    if args.as_json:
        print(json.dumps({"measurements": results, "live": live_results},
                         ensure_ascii=False, indent=2))
    else:
        print(render(results, live_results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
