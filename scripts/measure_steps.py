"""Donde se va el tiempo de un recorrido, fase por fase.

Un paso tarda lo que tarda por cuatro razones distintas, y el total no dice
cual: pensar (una peticion al modelo), escribir el valor de un campo (otra
peticion, a otro modelo), pulsar, y volver a mirar — que a su vez son esperar a
que la pagina se quede quieta y capturarla.

Importa porque las cuatro se arreglan de formas opuestas. Si el tiempo esta en
pensar, la palanca es pedir menos decisiones: planificar varios pasos con una
inferencia y repetir los conocidos sin preguntar. Si esta en esperar, la palanca
esta en el navegador y ninguna mejora del modelo la toca. Optimizar la que no
pesa es trabajo que no se nota.

Sale una tabla por paso y un reparto del total. Con `--json`, lo mismo para un
script que quiera comparar dos corridas.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_ultrafast.agent import Agent  # noqa: E402

FASES = [
    ("latency_ms", "pensar"),
    ("text_latency_ms", "escribir"),
    ("act_ms", "pulsar"),
    ("settle_ms", "esperar"),
    ("capture_ms", "capturar"),
]


def phases(step):
    return {nombre: step.get(clave) or 0 for clave, nombre in FASES}


def summarise(history, elapsed_ms, status):
    totals = {nombre: 0 for _clave, nombre in FASES}
    for step in history:
        for nombre, valor in phases(step).items():
            totals[nombre] += valor
    accounted = sum(totals.values())
    return {
        "status": status,
        "steps": len(history),
        "model_calls": sum(1 for s in history if s.get("latency_ms")),
        "text_calls": sum(1 for s in history if s.get("text_latency_ms")),
        "elapsed_ms": elapsed_ms,
        "by_phase_ms": totals,
        # Lo que el reloj de pared marco y ninguna fase reclama: arranque del
        # navegador, la primera observacion, los reintentos por pagina rancia.
        # Se declara en vez de repartirse, porque repartirlo seria inventarlo.
        "unaccounted_ms": max(0, elapsed_ms - accounted),
    }


def render(result):
    lines = ["| # | Accion | Pensar | Escribir | Pulsar | Esperar | Capturar |",
             "|---:|---|---:|---:|---:|---:|---:|"]
    for step in result["history"]:
        medido = phases(step)
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            step.get("step"), (step.get("action") or "")[:44],
            medido["pensar"], medido["escribir"], medido["pulsar"],
            medido["esperar"], medido["capturar"]))
    resumen = result["summary"]
    lines.append("")
    lines.append("**{}** en {} pasos, {:,} ms de reloj.".format(
        resumen["status"], resumen["steps"], resumen["elapsed_ms"]))
    lines.append("")
    lines.append("| Fase | ms | % del reloj |")
    lines.append("|---|---:|---:|")
    reloj = max(1, resumen["elapsed_ms"])
    for nombre, valor in sorted(resumen["by_phase_ms"].items(), key=lambda r: -r[1]):
        lines.append("| {} | {:,} | {:.1f} |".format(nombre, valor, valor * 100 / reloj))
    lines.append("| _sin atribuir_ | {:,} | {:.1f} |".format(
        resumen["unaccounted_ms"], resumen["unaccounted_ms"] * 100 / reloj))
    lines.append("")
    lines.append("Peticiones al modelo: {} de decision, {} de texto, para {} pasos.".format(
        resumen["model_calls"], resumen["text_calls"], resumen["steps"]))
    cache = result.get("cache")
    if cache:
        lines.append("")
        lines.append("Cache ({}): {} repetidas, {} nuevas, {} recuperadas, {} guardadas en {}".format(
            cache["mode"], cache["hits"], cache["misses"], cache["healed"],
            cache["entries"], cache["file"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="measure-steps", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--reuse-tab")
    parser.add_argument("--viewport", default="fixed")
    parser.add_argument("--upload-file", action="append", default=[])
    parser.add_argument("--scope", help="acotar la observacion a este contenedor")
    parser.add_argument("--ignore", help="selectores separados por coma que se excluyen")
    parser.add_argument("--cache-dir", help="donde guardar y leer las decisiones de este recorrido")
    parser.add_argument("--heal", action="store_true",
                        help="si lo guardado deja de encajar, volver a preguntar y reescribirlo. "
                             "Sin esto el recorrido PARA, que es lo que se quiere en pruebas")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    stopped = None
    agent = Agent(args.url, args.goal, reuse_target=args.reuse_tab, viewport=args.viewport,
                  upload_files=args.upload_file or None, scope=args.scope,
                  ignore=[s.strip() for s in (args.ignore or "").split(",") if s.strip()] or None,
                  cache_dir=args.cache_dir, heal=args.heal)
    try:
        for _snapshot in agent.run():
            pass
    except Exception as parada:
        # Una corrida que se detiene sigue teniendo una medida valida de lo que
        # alcanzo a hacer. Tirarla y no decir nada seria perder el dato justo en
        # el caso interesante. Pero sale por codigo de error: imprimir el motivo y
        # terminar en cero convierte un recorrido que no se completo en un exito a
        # ojos de quien lo llamo, que es justo el fallo que el modo estricto de la
        # cache existe para no cometer.
        stopped = parada
        print("La corrida se detuvo: {}".format(parada), file=sys.stderr)
    finally:
        state = agent.state
        result = {
            "url": args.url,
            "goal": args.goal,
            "history": state["history"],
            "summary": summarise(state["history"], state["elapsed_ms"], state["status"]),
        }
        if agent.decisions is not None:
            result["cache"] = agent.decisions.report()
        agent.close()

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 1 if stopped is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
