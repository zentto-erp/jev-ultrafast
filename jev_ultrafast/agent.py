"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage
from .decisions import Decisions, Diverged, resolve
from .model import action_space, choose, field_context, field_text
from .questions import MAX_MODEL_CALLS, MAX_STEPS


class Agent:
    # Sin cache por defecto, y declarado en la clase y no solo en el constructor:
    # hay pruebas que montan un agente sin pasar por `__init__` para ejercitar un
    # solo comando, y un atributo que solo existe si se construyo entero
    # convierte una capacidad opcional en un requisito escondido.
    decisions = None

    def __init__(self, url, goals, *, record_dir=None, screenshots=False, reuse_target=None,
                 viewport="fixed", upload_files=None, scope=None, ignore=None,
                 cache_dir=None, heal=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        # Files the run may attach when the model chooses UPLOAD. The model can
        # decide *that* a file goes in a control, never *which* file — a path is
        # not something to invent — so it is provided here and validated up front.
        self.upload_files = [str(Path(f).resolve()) for f in (upload_files or [])]
        missing = [f for f in self.upload_files if not Path(f).is_file()]
        if missing:
            raise ValueError(f"upload file not found: {missing[0]}")
        # `reuse_target` drives an existing tab instead of opening one, for when
        # a person is watching several runs in a row and the work should stay
        # where they are looking.
        # `scope` acota la observacion a un contenedor e `ignore` excluye zonas.
        # En una pantalla de ERP la cabecera, el menu y las pestanas del modulo no
        # son contexto cuando el trabajo esta en la rejilla: son opciones que no
        # pueden ser correctas compitiendo por el tope y por la decision.
        self.browser = Browser(url, reuse_target=reuse_target, viewport=viewport,
                               scope=scope, ignore=ignore)
        # Las decisiones guardadas de recorridos anteriores. Sin directorio no hay
        # cache: repetir sin haber guardado nada no es mas rapido, es adivinar.
        self.decisions = Decisions(cache_dir, heal=heal) if cache_dir else None
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def recall_or_choose(self, page, goal, history):
        """Lo decidido antes para esta pantalla, o una decision nueva.

        Sin cache, esto es exactamente lo que habia: una peticion por paso. Con
        cache, la pregunta solo se hace la primera vez que el recorrido pasa por
        aqui, y las siguientes se resuelven contra la observacion fresca leyendo
        la etiqueta, como lo haria una persona.
        """
        if self.decisions is None:
            return choose(page, goal, history)

        # El paso dentro del recorrido, que es lo que identifica la decision. Un
        # recorrido es una secuencia, no un mapa de pantallas.
        step = len(history)
        remembered = self.decisions.look_up(goal, step)
        if remembered:
            found = resolve(page, remembered)
            if found is not None:
                self.decisions.hits += 1
                return self.replayed(remembered, found)
            # Lo guardado ya no encaja. Aqui se separan los dos usos, y es el unico
            # sitio del motor donde la misma situacion tiene dos respuestas
            # legitimas segun para que se este usando.
            if not self.decisions.heal:
                raise Diverged(remembered, page)
            self.decisions.healed += 1
            self.decisions.forget(goal, step)
        else:
            self.decisions.misses += 1

        decision = choose(page, goal, history)
        # Se guarda con la accion ya resuelta, no con el id: un id no sobrevive al
        # siguiente render y lo que se quiere recordar es QUE se pulso.
        chosen = next((a for a in page["actions"] if a["id"] == decision["choice"]), None)
        self.decisions.remember(goal, step, decision, chosen, url=page.get("url"))
        return decision

    def replayed(self, remembered, found):
        """Una decision repetida, con la forma de una decidida — y sin fingir que se juzgo.

        `probability` y `confidence` se quedan en `None` a proposito. Poner 1.0
        seria comodo para quien lee el informe y falso: no hubo juicio, hubo una
        entrada de cache. Un numero inventado ahi convierte el registro de la
        corrida en algo que no se puede auditar.
        """
        choice = found if isinstance(found, str) else found["id"]
        return {
            "choice": choice,
            "operation": remembered["operation"],
            "target": None,
            "confidence": None,
            "probabilities": {choice: None},
            "operation_probabilities": {},
            "target_probabilities": {},
            "target_confidence": None,
            "raw_answers": {},
            "model": "recordado",
            "usage": {},
            "latency_ms": 0,
            "from_cache": True,
            "remembered_text": remembered.get("text"),
            "request": None,
        }

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            # El presupuesto cuenta PETICIONES, no pasos: una decision recordada no
            # llamo a nadie, y gastar su cupo haria que un recorrido cacheado se
            # agotara igual que uno nuevo — que es precisamente lo que la cache
            # existe para evitar.
            asked = sum(1 for d in state["decisions"] if not d.get("from_cache"))
            if asked >= MAX_MODEL_CALLS:
                raise ValueError(f"Reached the {MAX_MODEL_CALLS}-call model budget (JEV_MAX_MODEL_CALLS)")
            state["decision"] = self.recall_or_choose(state["page"], state["goal"], state["history"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action budget (JEV_MAX_STEPS)")
            text, helper = None, None
            if action["kind"] == "tab":
                # Cambiar de pestana no es una entrada sobre el DOM: no hay nodo
                # que pulsar ni valor que escribir, y la pagina de destino es
                # otra. Por eso no pasa por `Browser.act`, que comprueba que el
                # nodo decidido siga siendo el mismo — aqui no hay nodo.
                acted_at = time.perf_counter()
                state["browser"].switch(action["target"])
                act_ms = round((time.perf_counter() - acted_at) * 1000)
            elif action["kind"] == "upload":
                # UPLOAD is a directed operation, not a click: the file input is
                # usually hidden and its native OS dialog is undriveable, so the
                # file is handed straight to the input. It needs a file provided
                # to the run — without one there is nothing to attach.
                if not self.upload_files:
                    state["status"] = "blocked"
                    raise ValueError(
                        "UPLOAD was chosen but no file was provided to the run "
                        "(--upload-file). Nothing uploaded."
                    )
                if not state["browser"].fresh(page, action):
                    raise StalePage("Page changed before upload. Choose again.")
                acted_at = time.perf_counter()
                state["browser"].upload(action["node"], self.upload_files)
                act_ms = round((time.perf_counter() - acted_at) * 1000)
                state["browser"].after_input = action
            else:
                if action["kind"] == "fill":
                    if not state["browser"].fresh(page):
                        raise StalePage("Page changed before text generation. Choose again.")
                    if decision.get("remembered_text") is not None:
                        # El valor tambien estaba decidido. Volver a generarlo es
                        # la segunda peticion del paso, y puede salir distinta —
                        # con lo que la repeticion dejaria de ser una repeticion.
                        text = decision["remembered_text"]
                    else:
                        context = field_context(state["goal"], action, page, state["history"])
                        if self.pending_text and self.pending_text[0] == context:
                            _, text, helper = self.pending_text
                        else:
                            text, helper = field_text(context)
                            self.pending_text = (context, text, helper)
                            state["text_calls"].append(
                                {**helper, "field": action["label"], "value": text})
                # Browser.act checks freshness immediately before input, including after text generation.
                acted_at = time.perf_counter()
                state["browser"].act(action, page, text=text)
                act_ms = round((time.perf_counter() - acted_at) * 1000)
            self.pending_text = None
            if self.decisions is not None and not decision.get("from_cache"):
                # Ahora si se sabe el valor que se escribio, asi que la entrada se
                # completa con el. Sin esto la repeticion ahorra la decision pero
                # sigue pagando la generacion del texto en cada corrida.
                self.decisions.remember(state["goal"], len(state["history"]), decision, action,
                                        text=text, url=page.get("url"))
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    # Las cuatro cosas que consumen un paso, separadas: pensar
                    # (`latency_ms`), escribir el valor (`text_latency_ms`),
                    # pulsar (`act_ms`) y volver a mirar (`settle_ms` mas
                    # `capture_ms`, que llegan del observe de abajo). Un total
                    # solo dice que el paso tardo; esto dice por que.
                    "act_ms": act_ms,
                    "settle_ms": None,
                    "capture_ms": None,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            observed = state["page"].get("timing") or {}
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
                settle_ms=observed.get("settle_ms"),
                capture_ms=observed.get("capture_ms"),
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            # Dos formas de no avanzar, y hasta ahora solo se detectaba una.
            #
            # La primera: la pagina no cambia. Tres pasos seguidos sin efecto.
            #
            # La segunda: la pagina SI cambia y aun asi no se avanza, porque lo
            # que se esta haciendo es abrir y cerrar lo mismo. Un desplegable que
            # se despliega cuenta como cambio de pagina, asi que el detector de
            # arriba no lo ve nunca. Medido en el ERP: SESENTA pulsaciones
            # seguidas sobre el mismo combobox, agotando el presupuesto del
            # recorrido, mientras el control que abria el selector de verdad
            # estaba justo al lado sin tocar.
            #
            # Se mira el mismo control elegido cinco veces seguidas y no tres:
            # hay pasos legitimos que repiten control —escribir, borrar y volver
            # a escribir en un campo— y cortar a la tercera los rompe.
            repeated = state["history"][-3:]
            atascado = state["history"][-5:]
            state["status"] = (
                "blocked"
                if (len(repeated) == 3
                    and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated))
                or (len(atascado) == 5
                    and len({h["choice"] for h in atascado}) == 1
                    and atascado[-1]["kind"] != "wait")
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
