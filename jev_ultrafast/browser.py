"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# CDP modifier bits: Alt 1, Ctrl 2, Meta 4, Shift 8.
MODIFIERS = (("Alt", "AltLeft", 1), ("Control", "ControlLeft", 2),
             ("Meta", "MetaLeft", 4), ("Shift", "ShiftLeft", 8))
_MODIFIER_BITS = {name.lower(): bit for name, _code, bit in MODIFIERS}
# What people call them. `cmd` and `option` are the Mac names for the same keys.
_MODIFIER_BITS.update({"ctrl": 2, "cmd": 4, "command": 4, "option": 1, "opt": 1})


# Keys that mean something on their own. CDP wants the virtual key code as well
# as the name: without it Chrome delivers an event the page does not recognise,
# which is why "press Escape" appeared not to work rather than to fail.
NAMED_KEYS = {
    "escape": ("Escape", "Escape", 27), "esc": ("Escape", "Escape", 27),
    "enter": ("Enter", "Enter", 13), "return": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "space": (" ", "Space", 32),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46), "del": ("Delete", "Delete", 46),
    "home": ("Home", "Home", 36), "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33), "pagedown": ("PageDown", "PageDown", 34),
    "up": ("ArrowUp", "ArrowUp", 38), "down": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37), "right": ("ArrowRight", "ArrowRight", 39),
    "arrowup": ("ArrowUp", "ArrowUp", 38), "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37), "arrowright": ("ArrowRight", "ArrowRight", 39),
}
NAMED_KEYS.update({f"f{n}": (f"F{n}", f"F{n}", 111 + n) for n in range(1, 13)})


def key_spec(name):
    """Resolve a key name, or refuse it.

    An unknown name must not be sent as a one-character key: pressing the letter
    "e" when the case asked for "escape" closes nothing, changes nothing, and
    reads as the key not working.
    """
    resolved = NAMED_KEYS.get(str(name).strip().lower())
    if resolved:
        return resolved
    text = str(name)
    if len(text) == 1:
        return (text, None, ord(text.upper()))
    raise ValueError(f"Unknown key {name!r}; use escape, enter, tab, f2, up… or a single character")


# Installed before anything is clicked, and again on every new document so a
# navigation does not quietly disarm it. Idempotent: re-running replaces the
# record, never wraps a wrapper.
_ARM_SOURCE = """(() => {
  const seen = window.__jevSeen ||= {console: [], dialogs: []};
  if (!seen.original) {
    seen.original = {alert: window.alert, confirm: window.confirm, prompt: window.prompt,
                     error: console.error, warn: console.warn};
  }
  const accept = %s, reply = %s;
  const note = (kind, message, extra) => {
    seen.dialogs.push({kind, message: String(message ?? '').slice(0, 500), answered: extra});
    if (seen.dialogs.length > 20) seen.dialogs.shift();
  };
  window.alert = (message) => { note('alert', message, 'ok'); };
  window.confirm = (message) => { note('confirm', message, accept ? 'accept' : 'dismiss'); return accept; };
  window.prompt = (message) => { note('prompt', message, accept ? reply : null); return accept ? reply : null; };
  const record = (level) => (...args) => {
    seen.console.push({level, text: args.map(a => {
      try { return typeof a === 'string' ? a : JSON.stringify(a); } catch { return String(a); }
    }).join(' ').slice(0, 600)});
    if (seen.console.length > 40) seen.console.shift();
    seen.original[level].apply(console, args);
  };
  console.error = record('error');
  console.warn = record('warn');
  window.addEventListener('error', (e) => {
    seen.console.push({level: 'uncaught', text: String(e.message || e.error).slice(0, 600)});
    if (seen.console.length > 40) seen.console.shift();
  });
  window.addEventListener('unhandledrejection', (e) => {
    seen.console.push({level: 'unhandled', text: String(e.reason?.message || e.reason).slice(0, 600)});
    if (seen.console.length > 40) seen.console.shift();
  });
  return true;
})()"""


_INTERACTIVE_NODES = {"BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"}


def _attributes(node):
    """CDP hands attributes back as a flat [name, value, name, value…] list."""
    flat = node.get("attributes") or []
    return dict(zip(flat[0::2], flat[1::2]))


def _node_text(node, budget=60):
    """Whatever this node reads as, for naming it."""
    if node.get("nodeType") == 3:
        return (node.get("nodeValue") or "").strip()
    parts = []
    for child in node.get("children") or []:
        parts.append(_node_text(child, budget))
        if sum(len(p) for p in parts) > budget:
            break
    return " ".join(p for p in parts if p).strip()


def _name_of(node):
    attrs = _attributes(node)
    return (attrs.get("aria-label") or _node_text(node) or attrs.get("title")
            or attrs.get("placeholder") or attrs.get("name") or attrs.get("id") or "")


def closed_roots(document_root):
    """Every closed shadow root, and the controls sealed inside it.

    A closed root is private to script by design: `element.shadowRoot` is null,
    so the observation — which runs as script in the page — cannot see in, and
    the content is not merely unreachable but *unreported*. That is the worst
    shape a blind spot can take: the run answers confidently about a screen
    whose other half it never knew was there.

    CDP is not script and is not bound by that rule: `DOM.getDocument` with
    `pierce` returns closed roots like any other. Measured against a
    deliberately closed root: script said there was no shadow root at all, CDP
    returned it with the button inside.

    Nothing is opened and nothing is patched. The page behaves exactly as its
    author intended; this only refuses to pretend the content is not there.
    """
    found = []

    def collect(node, into):
        attrs = _attributes(node)
        if node.get("nodeName") in _INTERACTIVE_NODES or attrs.get("role") or attrs.get("onclick"):
            label = _name_of(node)
            if label or node.get("nodeName") in _INTERACTIVE_NODES:
                into.append({
                    "kind": (attrs.get("role") or node.get("nodeName", "")).lower(),
                    "label": label[:60],
                    # The handle CDP itself uses. Geometry and clicks resolve
                    # from it, so nothing here is a selector and nothing is a
                    # guess about the page's structure.
                    "backend": node.get("backendNodeId"),
                })
        for child in node.get("children") or []:
            collect(child, into)
        for root in node.get("shadowRoots") or []:
            collect(root, into)

    def walk(node, host=""):
        mine = _name_of(node) or host
        for root in node.get("shadowRoots") or []:
            if root.get("shadowRootType") == "closed":
                inside = []
                collect(root, inside)
                found.append({"host": (mine or node.get("nodeName", "")).strip()[:60],
                              "inside": inside})
            walk(root, mine)
        for child in node.get("children") or []:
            walk(child, mine)

    walk(document_root)
    return found


def _arm_record(target):
    """Where the arming choice for one tab is kept between steps."""
    folder = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp") / "jev-armed"
    # The target id comes from the caller, so it is checked rather than trusted:
    # it names a file, and a name with a separator in it names another folder.
    safe = "".join(c for c in str(target or "") if c.isalnum())
    return (folder / f"{safe}.json") if safe else None


def remember_arm(target, dialogs, text):
    record = _arm_record(target)
    if not record:
        return
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"dialogs": dialogs, "text": text}), encoding="utf-8")


def recall_arm(target):
    record = _arm_record(target)
    if not record or not record.exists():
        return None
    try:
        return json.loads(record.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def member_name(raw):
    """The property or method to reach on an element, checked before it travels.

    It arrives as a NAME and is used as a property name, never evaluated. The
    check is what keeps it that way: anything that is not a plain identifier —
    a path, a call, a semicolon — is refused here rather than being sent to the
    page and hoping it means nothing there.
    """
    if not isinstance(raw, str) or not raw.isidentifier():
        raise ValueError("`member` must be a plain property or method name")
    # Reaching the prototype plumbing is never the point and is how a name stops
    # being just a name.
    if raw.startswith("__") or raw in {"constructor", "prototype"}:
        raise ValueError(f"`member` cannot be {raw!r}")
    return raw


def modifier_mask(names):
    """Turn ["ctrl", "alt"] into the bitmask CDP expects.

    Unknown names are refused rather than ignored: a run that asks for
    `ctrl+shift` and silently gets a plain click reports a pass for a gesture
    that never happened, which is worse than failing.
    """
    if not names:
        return 0
    if isinstance(names, str):
        names = [names]
    mask = 0
    for name in names:
        bit = _MODIFIER_BITS.get(str(name).strip().lower())
        if bit is None:
            raise ValueError(f"Unknown modifier {name!r}; use alt, ctrl, meta or shift")
        mask |= bit
    return mask


def snapshot_budgets():
    """Caps the observation applies, overridable per run.

    The defaults are the historical ones. They are generous for an ordinary page
    and tight for a dense enterprise table, where the run can hit them long
    before the page is exhausted and report one screenful as if it were all
    there is. A value that is not a positive number is ignored rather than
    obeyed: a typo must not silently remove a cap.
    """
    names = {"actions": "JEV_MAX_ACTIONS", "text": "JEV_MAX_TEXT",
             "scrollers": "JEV_MAX_SCROLLERS"}
    chosen = {}
    for key, variable in names.items():
        raw = os.environ.get(variable)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            chosen[key] = value
    return chosen


def harvest_script():
    """El script que descubre lo que la pagina repite."""
    return (Path(__file__).parent / "harvest.js").read_text(encoding="utf-8")


def read_state_script():
    """The observation, with this run's budgets attached."""
    source = Path(__file__).with_name("snapshot.js").read_text()
    return f"(() => {{ window.__jevBudgets={json.dumps(snapshot_budgets())}; return {source}; }})()"


# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = read_state_script()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

def page_targets():
    """Las pestanas de aplicacion abiertas ahora mismo, en el orden que da Chrome.

    Se dejan fuera las pantallas del propio navegador. Existen y son targets,
    pero no son pantallas de la aplicacion: ofrecerlas a un agente solo le da
    opciones que nunca son la correcta.
    """
    # Una respuesta sin `targetInfos` es "no lo se", NO "no hay ninguna". Devolver
    # una lista vacia ahi seria convertir el desconocimiento en un dato: quien
    # pregunte concluira que el navegador esta limpio, y entonces cualquier
    # pestana que ya estuviera abierta pasara por recien aparecida. Se levanta y
    # que decida cada sitio como degradar, porque no degradan igual — el
    # constructor se queda sin foto inicial, la observacion no ofrece pestanas.
    answer = cdp("Target.getTargets")
    if "targetInfos" not in answer:
        raise LookupError("Target.getTargets no devolvio targetInfos")
    pages = []
    for info in answer["targetInfos"]:
        if info.get("type") != "page":
            continue
        if info.get("url", "").startswith(("devtools://", "chrome://", "chrome-extension://")):
            continue
        pages.append(info)
    return pages


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url, *, reuse_target=None, viewport="fixed"):
        ensure_daemon()
        # Las pestanas que ya estaban abiertas antes de empezar son del usuario,
        # y el recorrido no tiene nada que hacer en ellas.
        #
        # Medido al probar esto contra el Chrome del perfil de pruebas: habia
        # QUINCE pestanas de sesiones anteriores —X, Facebook, LinkedIn, Meta
        # Business Suite— y la observacion las ofrecia todas. Eso no es una
        # capacidad nueva, es quince opciones de ruido en el espacio de
        # decision, cada una de ellas incorrecta, compitiendo con los controles
        # de la pantalla que si importan. Empeoraba la decision.
        #
        # Lo que el agente necesita saber no es que pestanas existen: es cual
        # acaba de aparecer, porque eso es lo que hizo el click anterior.
        try:
            self.tabs_before = {info["targetId"] for info in page_targets()}
        except Exception:
            # `None` y no un conjunto vacio: son dos cosas distintas y la
            # diferencia importa. Un conjunto vacio significa "no habia ninguna
            # pestana antes", y entonces TODAS aparecieron durante el recorrido
            # —incluidas las quince del usuario—, que es justo el ruido que este
            # filtro existe para quitar. `None` significa "no lo se", y ante eso
            # solo se ofrecen las que abrimos nosotros, que es la unica cosa que
            # sabemos sin preguntarle al navegador.
            self.tabs_before = None
        # A fresh background tab per run is right for benchmarks: runs stay
        # isolated and the user's Chrome never steals focus. It is wrong when a
        # person is watching a sequence of runs, because every run opens another
        # tab and whatever they were looking at is no longer where the work
        # happens. `reuse_target` drives the existing tab instead.
        self.target = reuse_target or cdp(
            "Target.createTarget", url="about:blank", background=True
        )["targetId"]
        # Que pestanas son nuestras, no si LA pestana es nuestra. En cuanto un
        # recorrido puede abrir otra y cambiarse a ella, una sola bandera ya no
        # sabe responder: al cerrar habria que cerrar la que abrimos y dejar en
        # pie la prestada, y con un booleano se pierde una de las dos.
        self.owned = set() if reuse_target else {self.target}
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        # Se recuerda porque la emulacion es por sesion: al cambiar de pestana
        # hay que volver a aplicarla, y sin guardarla no hay que aplicar.
        self._viewport = viewport
        self._apply_viewport(viewport)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        # No url means "carry on from whatever this tab is showing".
        #
        # Chaining runs otherwise defeats itself: the second one navigates to the
        # same address and throws away the state the first one just built — a
        # filter cleared, a dialog opened, a row selected. The control under test
        # frequently does not exist until that state is there, so re-navigating
        # guarantees the run cannot find it.
        if url:
            self.call("Page.navigate", url=url)
        # Antes del primer observe: ese observe ya captura, y una ventana que no
        # se dibuja cuelga la captura.
        self.ensure_composited()
        self.wait_until_settled()

    def ensure_composited(self):
        """Make sure the window can paint, or screenshots hang.

        Chrome stops compositing a window it is not drawing, and
        `Page.captureScreenshot` then never returns. The harness reports that as
        "timed out waiting for the daemon", which points at the wrong thing: the
        daemon is fine, the window cannot paint.

        It reads like flakiness that worsens over a long session, and the reason
        is mundane — the longer a run lasts, the likelier the window ended up
        minimized or behind another one. Measured on about:blank:
        `Runtime.evaluate` answered in 0.0s while the capture timed out every
        time; with the window drawn again, the same capture took 0.1s.

        `windowState` alone is not enough: a window can report `maximized` and
        still be fully covered. `Page.bringToFront` is what actually guarantees
        it, so it runs once when the page is attached rather than on every
        observation — enough to keep captures working, without yanking focus
        away from whoever is watching on every step.
        """
        try:
            window = cdp("Browser.getWindowForTarget", targetId=self.target)
            if window.get("bounds", {}).get("windowState") == "minimized":
                cdp(
                    "Browser.setWindowBounds",
                    windowId=window["windowId"],
                    bounds={"windowState": "normal"},
                )
        except Exception:
            pass  # Not every target has a window (headless, some embedders).
        try:
            self.call("Page.bringToFront")
        except Exception:
            pass

    def _apply_viewport(self, viewport):
        """Fixed 1120x780, or the window the person is actually looking at.

        A pinned viewport makes benchmark runs comparable, and that is why it is
        the default. But when someone is watching, it leaves most of a wide
        window blank and — worse — hides what a responsive layout does at the
        real width: a table that only shows its last columns above 1400px is
        never seen, so the run cannot find a problem there.

        "window" measures the tab and matches the override to it.
        A page being driven on someone else's behalf — recorded, shared, or
        mid-workflow — should keep the size its owner set: "none" leaves it
        alone. Overriding it there would change what the recording shows.
        """
        if viewport in (None, "none"):
            return
        if viewport == "window":
            size = self.evaluate(
                "(() => [window.innerWidth || 0, window.innerHeight || 0])()"
            )
            if isinstance(size, list) and len(size) == 2 and all(size):
                width, height = int(size[0]), int(size[1])
                # An override of 0 disables emulation entirely, which is what we
                # want if the tab could not report a usable size.
                self.call(
                    "Emulation.setDeviceMetricsOverride",
                    width=width, height=height, deviceScaleFactor=1, mobile=False,
                )
                return
            self.call("Emulation.clearDeviceMetricsOverride")
            return

        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=1120, height=780, deviceScaleFactor=1, mobile=False,
        )

    def wait_until_settled(self, timeout=15, quiet_for=0.4):
        """Wait for the document to load AND for the DOM to stop changing.

        `readyState == "complete"` fires when the document and its subresources
        are done, which on a single-page app is before the first render that
        carries data: the framework has only just been handed control. Observing
        there returns the empty shell, so an assertion reads a page the user
        never saw and a run reports a failure the application does not have.

        Waiting for a short quiet period in the DOM costs a few hundred
        milliseconds and removes that whole class of false negative.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

        last, stable_since = None, None
        while time.monotonic() < deadline:
            # Length is a cheap proxy for "the DOM changed". A real mutation
            # observer would need an injected script surviving navigations, for
            # a signal no better at deciding when to look.
            size = self.evaluate("document.body ? document.body.innerHTML.length : 0")
            now = time.monotonic()
            if size == last and size:
                if stable_since and now - stable_since >= quiet_for:
                    return
            else:
                last, stable_since = size, now
            time.sleep(0.05)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def prearm_next_document(self):
        """Register the arming for the document that is about to load.

        `rearm_if_needed` is too late for a navigation: it runs once the page
        is already there, so the calls made while it was loading — which is
        most of them, and the ones that decide whether the screen has data —
        happened before anything was listening. Measured: armed, navigated,
        and an empty trail on a page that had just made a dozen requests.
        """
        wanted = recall_arm(self.target)
        if not wanted:
            return False
        try:
            self.call("Page.addScriptToEvaluateOnNewDocument",
                      source=_ARM_SOURCE % (json.dumps(wanted.get("dialogs") != "dismiss"),
                                            json.dumps(wanted.get("text") or "")))
            return True
        except Exception:
            return False

    def rearm_if_needed(self):
        """Put the arming back if the page came up without it.

        Cheap enough to do before every observation — one evaluate that
        returns a boolean — and the alternative is a trail that silently stops
        at the first navigation, which looks like an application that stopped
        making requests.
        """
        wanted = recall_arm(self.target)
        if not wanted:
            return False
        try:
            if self.evaluate("(() => !!window.__jevSeen)()"):
                return False
            source = _ARM_SOURCE % (json.dumps(wanted.get("dialogs") != "dismiss"),
                                    json.dumps(wanted.get("text") or ""))
            self.call("Page.addScriptToEvaluateOnNewDocument", source=source)
            self.call("Runtime.evaluate", expression=source)
            return True
        except Exception:
            # Re-arming is a convenience. Failing it must never take down the
            # step that was actually asked for.
            return False

    def observe(self, screenshot=True):
        # El reloj empieza aqui porque una observacion son dos cosas distintas
        # con causas distintas: esperar a que la pagina se quede quieta, y
        # capturarla. Sumadas dan un numero que no dice donde mirar — y en una
        # aplicacion de micro-frontends la espera puede ser diez veces la
        # captura sin que nada este roto.
        started = time.perf_counter()
        self.rearm_if_needed()
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
            # Esos 50 ms y dos frames bastan en una web ligera y no en una que
            # monta un dialogo con una tabla entera al pulsar un boton: se
            # observa a mitad del montaje, la decision sale sobre una pagina que
            # ya cambio, y el paso se repite hasta agotarse. Esperar a que el DOM
            # se quede quieto cuesta unas decimas y quita esa variabilidad.
            try:
                self.wait_until_settled(timeout=5, quiet_for=0.25)
            except Exception:
                pass
        settled = time.perf_counter()
        for attempt in range(10):
            try:
                page = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                # Los reintentos cuentan dentro de la captura a proposito: son
                # tiempo que el paso pago de verdad.
                page["timing"] = {
                    "settle_ms": round((settled - started) * 1000),
                    "capture_ms": round((time.perf_counter() - settled) * 1000),
                }
                self.offer_other_tabs(page)
                return page
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def offer_other_tabs(self, page):
        """Poner las demas pestanas en la observacion, como operaciones.

        Van despues del tope de acciones a proposito: una pestana no compite por
        sitio con los controles de la pantalla, y descartarla por un tope
        pensado para una tabla densa dejaria al agente sin ver donde ocurrio el
        trabajo.

        No entran en la huella. Que aparezca una pestana no cambia la pagina
        sobre la que se acaba de decidir, y tratarlo como cambio obligaria a
        repetir una decision que sigue siendo valida — con el efecto colateral
        de que una aplicacion que abre una ventana emergente al cargar dejaria
        el recorrido en bucle.
        """
        try:
            others = [tab for tab in self.tabs()
                      if not tab["driving"] and (tab["ours"] or tab["appeared"])]
        except Exception:
            # Saber que otras pestanas hay es una ayuda, no un requisito. Si el
            # navegador no contesta, la observacion sigue siendo buena.
            return page
        page["tabs"] = others
        for position, tab in enumerate(others, start=1):
            name = (tab["title"] or tab["url"] or "sin titulo").strip()
            # Se dice que se abrio durante el recorrido porque eso es la razon
            # para mirarla: casi siempre la abrio el paso anterior.
            page["actions"].append({
                "id": f"TAB_{position}",
                "kind": "tab",
                "node": None,
                "target": tab["target"],
                "label": f"Ir a la pestana abierta durante el recorrido: {name[:80]}",
                "url": tab["url"],
            })
        return page

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    # ─── Directed operations ──────────────────────────────────────────────
    #
    # `act` exists for the loop where the model picks the next move. These are
    # for the caller that already knows what it wants: a test case naming the
    # row to drag, the value to read, the key to hold.
    #
    # They were written and left unreachable — implemented in
    # `browser_operation` with nothing exposing them, so a run could not use
    # one even knowing it was there. A capability nobody can call is the same
    # as a capability that does not exist, and an agent reported exactly that:
    # "the engine has no inspect mode". It had one. It had no door.

    def inspect(self, node):
        """What an element holds, without touching it."""
        return browser_operation({"operation": "inspect", "session": self.session, "node": node})

    def listen(self, events):
        """Start recording the component's own events."""
        return browser_operation({"operation": "listen", "session": self.session, "events": events})

    def heard(self, clear=False):
        """What arrived since `listen`. Empty is an answer."""
        return browser_operation({"operation": "heard", "session": self.session, "clear": clear})

    def component(self, node, member, mode="get", value=None, args=None):
        """Read, set or call a named member of a custom element."""
        return browser_operation({"operation": "component", "session": self.session,
                                  "node": node, "member": member, "mode": mode,
                                  "value": value, "args": args or []})

    def hold(self, modifiers, settle=0.8):
        """Hold a modifier and observe while it is down."""
        return browser_operation({"operation": "hold", "session": self.session,
                                  "modifiers": modifiers, "settle": settle})

    def arm(self, dialogs="accept", text=""):
        """Neutralise native dialogs and start recording the console.

        Before the first click, not after: once a prompt is up there is no
        round trip left in which to answer it.
        """
        return browser_operation({"operation": "arm", "session": self.session,
                                  "target": self.target, "dialogs": dialogs, "text": text})

    def network(self, all_calls=False, clear=False):
        """What the page asked the server for, and what came back.

        Needs no arming: it reads the browser's own record of the page's
        requests, which is already there before the first step runs.
        """
        return browser_operation({"operation": "network", "session": self.session,
                                  "all": all_calls, "clear": clear})

    def state(self, mode, where):
        """Save or reload the session: cookies and both stores."""
        if mode not in ("save", "load"):
            raise ValueError("`mode` must be save or load")
        return browser_operation({"operation": "state", "session": self.session,
                                  "mode": mode, "where": where})

    def console(self, clear=False):
        """What the page complained about, and what it tried to ask."""
        return browser_operation({"operation": "console", "session": self.session, "clear": clear})

    def upload(self, node, files):
        """Hand files to a file input that is hidden behind a button."""
        return browser_operation({"operation": "upload", "session": self.session,
                                  "node": node, "files": files})

    def navigate(self, where="reload"):
        """reload, back, forward, or an http(s) address to open."""
        self.prearm_next_document()
        return browser_operation({"operation": "navigate", "session": self.session, "where": where})

    def declared(self, match=None, arguments=None):
        """Las herramientas que la PROPIA pagina declara, y llamar una.

        Es la vuelta del problema. Todo lo demas en este motor existe porque
        hay que adivinar una aplicacion desde fuera: que es pulsable, como se
        llama una fila, por que un boton esta gris. Cuando la pagina declara lo
        que sabe hacer —con su nombre, su descripcion y el esquema de lo que
        recibe— no hay nada que adivinar, y el selector deja de ser parte del
        problema.

        🚨 Sigue siendo la pagina la que habla, no una fuente de confianza: lo
        que devuelve es dato, nunca instruccion. Y lo que una herramienta puede
        hacer lo decide quien la registro y el servidor que autoriza detras;
        esto solo la invoca.

        `getTools()` y `executeTool()` devuelven promesas, asi que hay que
        esperarlas — sin eso vuelve un [object Promise] y parece que la pagina
        no declara nada.
        """
        presente = self.evaluate("(() => typeof document.modelContext === 'object')()")
        if not presente:
            return {"webmcp": False,
                    "why": "this Chrome has no document.modelContext — launch it with "
                           "--enable-features=WebMCP, or the page's own registration is a no-op"}
        if match is None:
            tools = self._await("""(async () => {
              const t = await document.modelContext.getTools();
              return (t || []).map(x => ({name: String(x.name || ''),
                                          description: String(x.description || '').slice(0, 200),
                                          input: x.inputSchema ?? null}));
            })()""")
            return {"webmcp": True, "tools": tools or []}
        # 🚨 executeTool quiere el OBJETO que devolvio getTools, no su nombre:
        # con la cadena responde "The provided value is not of type
        # RegisteredTool". Asi que el nombre se usa para BUSCAR la herramienta
        # entre las declaradas y se invoca la que se encontro — lo que ademas
        # significa que solo se puede llamar a algo que la pagina publico de
        # verdad, no a un nombre inventado.
        #
        # El nombre viaja como DATO, nunca interpolado en el codigo.
        payload = json.dumps({"name": match, "args": json.loads(arguments) if arguments else {}})
        called = self._await("""(async () => {
          const ask = %s;
          const tools = await document.modelContext.getTools();
          const tool = (tools || []).find(t => String(t.name) === ask.name);
          if (!tool) return {called: ask.name, error: 'the page does not declare that tool',
                             available: (tools || []).map(t => String(t.name))};
          try {
            // 🚨 Chrome quiere los argumentos como CADENA JSON, no como objeto:
            // con un objeto responde "Failed to parse input arguments", que
            // suena a que el esquema no cuadra y en realidad es el envoltorio.
            const out = await document.modelContext.executeTool(tool, JSON.stringify(ask.args));
            // Y devuelve otra cadena, con el sobre de MCP dentro. Sin
            // desenvolverlo lo que llega es un JSON escapado tres veces, que
            // tecnicamente es la respuesta y en la practica no la lee nadie.
            let valor = out;
            try {
              const sobre = typeof out === 'string' ? JSON.parse(out) : out;
              // El salto de linea va por codigo a proposito. Esto es JS dentro
              // de una cadena de Python: una secuencia de escape escrita aqui
              // la interpreta Python primero, y a la pagina llega un salto
              // REAL dentro de un literal, que no parsea. Vale hasta para un
              // comentario como este, que fue justo lo que lo rompio.
              const texto = sobre?.content?.map?.(c => c?.text ?? '').join(String.fromCharCode(10));
              if (texto !== undefined) { try { valor = JSON.parse(texto); } catch { valor = texto; } }
              else valor = sobre;
            } catch {}
            return {called: ask.name, result: valor ?? null};
          } catch (e) { return {called: ask.name, error: String(e && e.message || e)}; }
        })()""" % payload)
        return called or {"called": match, "result": None}

    def _await(self, expression):
        """Un evaluate que espera la promesa, con su error si la rechaza."""
        response = self.call("Runtime.evaluate", expression=expression,
                             returnByValue=True, awaitPromise=True)
        if response.get("exceptionDetails"):
            detail = response["exceptionDetails"].get("exception", {}).get("description")
            raise StalePage(detail or "The page rejected the call")
        return response.get("result", {}).get("value")

    def harvest(self, match=None):
        """Lo que la pantalla repite, como datos.

        Una lista de mercados, una tabla de precios, un tablero de noticias: en
        cuanto una pagina enseña muchas cosas del mismo tipo, lo que interesa ya
        no es que se puede pulsar sino QUE DICE, y el que pregunta las quiere
        todas, en una estructura que pueda recorrer un programa.

        `match` filtra por texto y se aplica DENTRO de cada grupo, no sobre el
        resultado: quedarse con el grupo entero y filtrar despues devolveria
        una lista de cuarenta para entregar dos.

        🚨 Solo lee. No pulsa, no escribe y no manda nada a ningun sitio.
        """
        data = self.evaluate(harvest_script())
        if not data:
            return {"groups": []}
        wanted = (match or "").strip().lower()
        if wanted:
            kept = []
            for group in data.get("groups", []):
                items = [one for one in group.get("items", [])
                         if wanted in " ".join(one.get("text", [])).lower()]
                if items:
                    kept.append({**group, "items": items, "shown": len(items)})
            data["groups"] = kept
            data["matched"] = match
        return data

    def sealed(self, match=None):
        """What is inside the closed components, and press one of them.

        Without `match` it lists them. With it, the first control whose label
        matches is clicked where it actually is on screen.

        The click goes through `DOM.getBoxModel`, which is CDP asking the
        renderer for the real rectangle — not a guess, and not a coordinate a
        model produced. A control with no box is off-screen or not rendered,
        and that is said rather than clicked at 0,0.
        """
        document = self.call("DOM.getDocument", depth=-1, pierce=True)
        roots = closed_roots(document["root"])
        if not match:
            return {"closed_roots": roots,
                    "note": "script cannot see into these; CDP can. Press one with --match."}
        wanted = match.strip().lower()
        for root in roots:
            for one in root["inside"]:
                if wanted in (one.get("label") or "").lower():
                    try:
                        box = self.call("DOM.getBoxModel", backendNodeId=one["backend"])["model"]
                    except Exception:
                        raise StalePage(
                            f"{one['label']!r} is inside a closed component and has no box "
                            "on screen — it is not rendered, or it is scrolled away.")
                    quad = box["content"]
                    x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
                    y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
                    for event in ("mousePressed", "mouseReleased"):
                        self.call("Input.dispatchMouseEvent", type=event, x=x, y=y,
                                  button="left", clickCount=1)
                    self.wait_until_settled()
                    return {"pressed": one["label"], "inside": root["host"], "at": [x, y]}
        raise SystemExit(json.dumps({
            "error": "not_found",
            "looked_for": match,
            "available": [one.get("label") for root in roots for one in root["inside"]][:40],
        }))

    def highlight(self, node):
        """Ring the element for a moment, for whoever is watching the window."""
        return browser_operation({"operation": "highlight", "session": self.session, "node": node})

    def key(self, name, modifiers=None):
        """Press a key that is not text: Escape, Enter, F2, an arrow."""
        return browser_operation({"operation": "key", "session": self.session,
                                  "key": name, "modifiers": modifiers or []})

    def hover(self, node, settle=0.25):
        """Move the pointer there for real, so `mousemove` listeners fire."""
        return browser_operation({"operation": "hover", "session": self.session,
                                  "node": node, "settle": settle})

    def drag(self, node, to=None, dx=0, dy=0, mode=None, steps=8, at_top=False):
        """Drag by pointer, or `mode="html5"` for elements with `draggable`."""
        return browser_operation({"operation": "drag", "session": self.session,
                                  "node": node, "to": to, "dx": dx, "dy": dy,
                                  "mode": mode, "steps": steps, "atTop": at_top})

    def context_menu(self, node):
        """Right click."""
        return browser_operation({"operation": "contextmenu", "session": self.session, "node": node})

    def touch(self, enabled=True, points=1):
        """Switch touch emulation mid-run; the tree changes with it."""
        return browser_operation({"operation": "touch", "session": self.session,
                                  "enabled": enabled, "points": points})

    # ─── Pestanas ─────────────────────────────────────────────────────────
    #
    # Un sistema web no cabe en una pestana. El ERP abre el PDF de la factura,
    # la vista de impresion o un selector en otra, y hasta ahora el agente se
    # quedaba mirando la que ya conocia y concluia que el boton no habia hecho
    # nada. No era un fallo de la aplicacion: la mitad del trabajo ocurria donde
    # nadie miraba.
    #
    # Se ofrecen al modelo como operaciones, igual que las teclas de funcion, y
    # por el mismo motivo: una pestana no es un elemento del DOM, asi que no
    # puede ser el objetivo de un click. Que aparezca una nueva es informacion
    # que cambia el siguiente paso, y por tanto tiene que estar en la
    # observacion y no en la cabeza de quien la lanzo.

    @property
    def owns_target(self):
        """Si la pestana que se conduce ahora es de las que abrimos."""
        return self.target in self.owned

    def tabs(self):
        """Las pestanas abiertas, con la que se conduce marcada.

        Se excluyen las de herramientas del navegador: existen, pero no son
        pantallas de la aplicacion y ofrecerlas solo da al modelo una opcion que
        nunca es la correcta.
        """
        return [{
            "target": info["targetId"],
            "url": info.get("url", ""),
            "title": info.get("title", ""),
            "driving": info["targetId"] == self.target,
            "ours": info["targetId"] in self.owned,
            # Aparecio despues de empezar, asi que este recorrido pudo causarla.
            # Sin foto inicial no se afirma: no saberlo no es que si.
            "appeared": (self.tabs_before is not None
                         and info["targetId"] not in self.tabs_before),
        } for info in page_targets()]

    def switch(self, target_id):
        """Conducir otra pestana, sin cerrar la que se deja.

        La que se abandona puede ser la del usuario y el paso siguiente puede
        querer volver, asi que cambiar de pestana no destruye nada. Tampoco se
        trae al frente: activarla robaria el foco de lo que la persona esta
        mirando, y el motor ya sabe pintar una pestana de fondo.
        """
        if target_id not in {tab["target"] for tab in self.tabs()}:
            raise ValueError(f"no hay ninguna pestana con id {target_id}")
        self.target = target_id
        self.session = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        # La emulacion se aplica por sesion: al cambiar hay que repetirla o la
        # pestana nueva se observa con la ventana en otro tamano, que es
        # exactamente el fallo que `viewport` existe para evitar.
        self._apply_viewport(self._viewport)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.ensure_composited()
        self.wait_until_settled()
        # Un dialogo armado lo estaba para la sesion anterior. Se rearma en la
        # siguiente observacion, que es donde ya se comprueba.
        self.after_input = None
        return {"driving": target_id, "tabs": self.tabs()}

    def open_tab(self, url=None, switch=True, own=True):
        """Abrir una pestana nueva, de fondo, y conducirla salvo que se diga que no.

        `own=False` la abre sin adoptarla, para quien no puede ser su dueno: la
        CLI es un proceso por paso, asi que una pestana suya moriria en el
        `finally` del mismo comando que la abrio — y abrir una pestana que se
        cierra sola no es abrir una pestana.
        """
        target_id = cdp("Target.createTarget", url=url or "about:blank", background=True)["targetId"]
        if own:
            self.owned.add(target_id)
        if switch:
            return self.switch(target_id)
        return {"opened": target_id, "tabs": self.tabs()}

    def close_tab(self, target_id=None):
        """Cerrar una pestana; si es la que se conduce, pasar a otra que quede.

        Quedarse conduciendo una pestana cerrada es un estado en el que todo
        falla con un error que habla de sesiones y no de lo que paso, asi que se
        resuelve aqui en vez de dejarlo para el primer paso siguiente.
        """
        target_id = target_id or self.target
        cdp("Target.closeTarget", targetId=target_id)
        self.owned.discard(target_id)
        if target_id != self.target:
            return {"closed": target_id, "tabs": self.tabs()}
        self.target = None
        remaining = [tab for tab in self.tabs() if tab["target"] != target_id]
        if not remaining:
            return {"closed": target_id, "driving": None, "tabs": []}
        return {"closed": target_id, **self.switch(remaining[-1]["target"])}

    def pdf(self, where):
        """La pantalla tal como se imprimiria, que es la evidencia que se archiva."""
        result = self.call("Page.printToPDF", printBackground=True)
        Path(where).write_bytes(base64.b64decode(result["data"]))
        return {"saved": str(Path(where).resolve()), "bytes": Path(where).stat().st_size}

    def close(self):
        # A borrowed tab is not ours to close: the caller is reusing it across
        # runs, and closing it would defeat the reason for lending it. Lo que se
        # cierra son las que abrimos nosotros, este conduciendo cual sea.
        for target in list(self.owned):
            try:
                cdp("Target.closeTarget", targetId=target)
            except Exception:
                # Una pestana ya cerrada —por la propia pagina, o por el
                # usuario— no es un fallo del cierre.
                pass
        self.owned.clear()
        self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "inspect":
        # Asking instead of acting. The observation is capped and summarised —
        # by design, or every step would carry the whole page — so the value a
        # check depends on may simply not be in it. Without a way to ask, a run
        # can only report that it clicked Save, never that the record exists.
        #
        # Read-only: it resolves a node the observation already found and
        # returns what it holds. It cannot navigate, type or click, so it can be
        # used freely to verify without changing what is being verified.
        node = request.get("node")
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        found = evaluate("""(node => {
          const e=window.__jevFast?.nodes.get(node);
          if (!e?.isConnected) return null;
          const r=e.getBoundingClientRect();
          const attrs={};
          for (const a of e.attributes||[]) attrs[a.name]=a.value.slice(0,200);
          return {
            tag:e.tagName.toLowerCase(),
            text:(e.innerText||e.textContent||'').replace(/\\s+/g,' ').trim().slice(0,4000),
            value:'value' in e ? String(e.value) : null,
            checked:e.checked??null,
            disabled:e.matches(':disabled'),
            visible:e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}),
            rect:{x:r.x,y:r.y,w:r.width,h:r.height},
            attributes:attrs,
          };
        })(%s)""" % json.dumps(node))
        if found is None:
            raise StalePage("That element is no longer in the document")
        return found

    if operation == "arm":
        # Two things that have to be in place BEFORE the click, not after.
        #
        # A native alert, confirm or prompt is not DOM. It blocks the page, and
        # every later step then times out against a document that cannot
        # answer: the run does not fail, it HANGS, and the cause is invisible
        # because there is nothing in the tree to find. Answering it over the
        # wire needs a round trip that no longer exists once it is up.
        #
        # So they are answered before they can block, by replacing the three
        # functions and recording what was asked. That also turns an invisible
        # wall into evidence: "the page asked to confirm X" is a finding.
        #
        # The console is the same idea. A run that clicked everything and saw
        # no visible error can still have left a trail of them, and a stack
        # trace is evidence a screenshot cannot give — but only if something
        # was listening when it happened.
        answer = request.get("dialogs", "accept")
        if answer not in ("accept", "dismiss"):
            raise ValueError("`dialogs` must be accept or dismiss")
        reply = json.dumps(request.get("text") or "")
        call("Page.addScriptToEvaluateOnNewDocument", source=_ARM_SOURCE % (
            json.dumps(answer == "accept"), reply))
        # And in the document already open, which the line above does not touch.
        evaluate(_ARM_SOURCE % (json.dumps(answer == "accept"), reply))
        # 🚨 That registration belongs to this CDP session, and this engine is
        # one process per step — so it dies with the process, and the very next
        # navigation comes up unarmed. Observed: `armed: true`, then a `goto`,
        # then `armed: false` and an empty trail.
        #
        # "Armed" has to mean armed, not armed until you go somewhere. So the
        # choice is remembered next to the tab it was made for, and re-applied
        # whenever the page comes back without it.
        remember_arm(request.get("target"), answer, request.get("text") or "")
        return {"armed": True, "dialogs": answer}

    if operation == "console":
        entries = evaluate("(() => (window.__jevSeen?.console ?? []).slice(-40))()")
        dialogs = evaluate("(() => (window.__jevSeen?.dialogs ?? []).slice(-20))()")
        if request.get("clear"):
            call("Runtime.evaluate", expression=
                 "(() => { if (window.__jevSeen) { window.__jevSeen.console=[]; window.__jevSeen.dialogs=[]; } })()")
        return {"entries": entries or [], "dialogs": dialogs or []}

    if operation == "network":
        # What the screen asked the server for, and what came back.
        #
        # A page can look perfectly right and be built on an answer that never
        # arrived: the table renders empty because the request 500'd, the total
        # is stale because the save was rejected. Nothing in the DOM says so, so
        # a run reports success on a screen that is quietly wrong — and the
        # reverse costs more, because a real defect gets blamed on the driver
        # when there is no way to tell "the app failed" from "the click missed".
        #
        # Read from Resource Timing, which the browser keeps on its own.
        #
        # 🚨 The obvious implementation — wrapping `fetch` and `XMLHttpRequest`
        # — was written first and thrown away, because it was measured and it
        # did not work: on a real screen it caught 4 calls out of 38. It saw the
        # session polling and missed every single `/v1` call the application
        # actually depends on. A wrapper only sees what goes through the exact
        # function it replaced, and a bundled application reaches the network
        # through paths that never touch it.
        #
        # Resource Timing has none of that: it is the browser's own record, so
        # it cannot be bypassed, it needs nothing installed before the page
        # loads, it survives every navigation, and it does not monkey-patch the
        # application under test — which is its own argument, since perturbing
        # what you are measuring is how a driver invents defects.
        #
        # What it does not carry is the request method or the body. The method
        # is a real loss and is not worth patching the page to recover; the
        # body was never going to be recorded anyway, because it carries the
        # session token and this ends up in evidence files.
        calls = evaluate("""(() => {
          // Default buffer is 250 entries and a long run silently outgrows it.
          try { performance.setResourceTimingBufferSize(1000); } catch {}
          return performance.getEntriesByType('resource')
            .filter(r => r.initiatorType === 'fetch' || r.initiatorType === 'xmlhttprequest')
            .slice(-120)
            .map(r => {
              let where = r.name, third = false;
              try {
                const u = new URL(r.name);
                // 🚨 El host solo se tira si es NUESTRO. Recortando siempre a
                // la ruta se pierde de quien era la llamada, y sin eso no se
                // puede distinguir un 500 de la aplicacion de un rastreador
                // que bloqueo el navegador — que es la diferencia entre un
                // hallazgo y ruido.
                // Comparar el ORIGEN entero deja fuera la propia API cuando
                // vive en un subdominio, que es lo normal: api.sitio.com
                // frente a app.sitio.com. Y justo esas son las llamadas cuyo
                // 500 hay que ver — clasificarlas como ajenas las saca del
                // veredicto y el informe dice que todo va bien.
                //
                // Se comparan las dos ultimas etiquetas del host. Es una
                // aproximacion: para un dominio con sufijo compuesto -.co.uk-
                // agrupa de mas, y eso es preferible a lo de antes, que
                // descartaba la API de todo el mundo.
                const raiz = (h) => h.split('.').slice(-2).join('.');
                third = raiz(u.hostname) !== raiz(location.hostname);
                where = third ? (u.host + u.pathname + u.search) : (u.pathname + u.search);
              } catch {}
              const status = r.responseStatus ?? null;
              return {url: where.slice(0, 200), status, ms: Math.round(r.duration), third_party: third,
                      // A zero or absent status is a request that never got an
                      // answer — refused, blocked by CORS, DNS gone. Those are
                      // the ones that leave the screen emptiest.
                      failed: status === null || status === 0 || status >= 400};
            });
        })()""") or []
        if not request.get("all"):
            calls = [one for one in calls if one.get("failed")]
        if request.get("clear"):
            call("Runtime.evaluate", expression="performance.clearResourceTimings()")
        return {"calls": calls}

    if operation == "state":
        # What makes a session a session: the cookies and the two stores.
        #
        # Without this every run starts at the login page, and the only ways
        # past it are worse than the problem — a password in a goal, or a human
        # sitting there to type it. Saved once by hand, reloaded from then on.
        #
        # 🚨 The file IS the session. Whoever holds it is logged in as that
        # user, so it belongs wherever a credential belongs and never in a
        # repository, an evidence folder or an attachment.
        where = request.get("where")
        if request.get("mode") == "save":
            cookies = call("Network.getCookies").get("cookies", [])
            stores = evaluate("""(() => {
              const dump = (s) => { try { return Object.entries({...s}); } catch { return []; } };
              return {origin: location.origin,
                      local: dump(localStorage), session: dump(sessionStorage)};
            })()""") or {}
            state = {"cookies": cookies, "origins": [stores]}
            Path(where).write_text(json.dumps(state, indent=2), encoding="utf-8")
            return {"saved": where, "cookies": len(cookies),
                    "local": len(stores.get("local") or []),
                    "session": len(stores.get("session") or [])}
        state = json.loads(Path(where).read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        if cookies:
            call("Network.setCookies", cookies=cookies)
        restored = 0
        for origin in state.get("origins") or []:
            # Only into the origin the entries came from: writing another
            # site's tokens into this one is how a "restore" turns into a leak.
            if origin.get("origin") and origin["origin"] != evaluate("location.origin"):
                continue
            evaluate("""((data) => {
              for (const [k, v] of data.local || []) { try { localStorage.setItem(k, v); } catch {} }
              for (const [k, v] of data.session || []) { try { sessionStorage.setItem(k, v); } catch {} }
              return true;
            })(%s)""" % json.dumps(origin))
            restored += len(origin.get("local") or []) + len(origin.get("session") or [])
        return {"loaded": where, "cookies": len(cookies), "entries": restored}

    if operation == "upload":
        # A file input is deliberately hidden and opened by a button, so there
        # is nothing to click and no dialog a driver can drive. The file has to
        # be handed to the element itself.
        node = request.get("node")
        files = request.get("files") or []
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        if not files or not all(isinstance(f, str) for f in files):
            raise ValueError("Pass `files` as a list of absolute paths")
        missing = [f for f in files if not Path(f).is_file()]
        if missing:
            raise ValueError(f"No such file: {missing[0]}")
        described = evaluate("""(node => {
          const e=window.__jevFast?.nodes.get(node);
          if (!e?.isConnected) return null;
          // The button is not the input. If what was observed is a button, the
          // input it opens is usually a hidden sibling or inside the same
          // control — say so rather than failing on the wrong element.
          const input = e.tagName === 'INPUT' && e.type === 'file' ? e
            : e.querySelector('input[type=file]') ||
              e.closest('*')?.querySelector('input[type=file]') || null;
          if (!input) return {wrong:true};
          return {ok:true, objectId:null};
        })(%s)""" % json.dumps(node))
        if described is None:
            raise StalePage("That element is no longer in the document")
        if described.get("wrong"):
            raise ValueError("That element is not a file input and does not contain one")
        remote = call("DOM.getDocument", depth=-1, pierce=True)
        # Resolve the actual input node for DOM.setFileInputFiles.
        target = evaluate("""(node => {
          const e=window.__jevFast?.nodes.get(node);
          const input = e.tagName === 'INPUT' && e.type === 'file' ? e : e.querySelector('input[type=file]');
          if (!input) return null;
          input.setAttribute('data-jev-upload', '1');
          return true;
        })(%s)""" % json.dumps(node))
        if not target:
            raise ValueError("Could not resolve the file input")
        found = call("DOM.querySelector", nodeId=remote["root"]["nodeId"],
                     selector="[data-jev-upload='1']")
        call("DOM.setFileInputFiles", files=files, nodeId=found["nodeId"])
        evaluate("""(() => { const e=document.querySelector('[data-jev-upload]');
                             e?.removeAttribute('data-jev-upload'); })()""")
        return {"uploaded": files}

    if operation == "navigate":
        where = request.get("where", "reload")
        if where == "reload":
            call("Page.reload")
        elif where in ("back", "forward"):
            history = call("Page.getNavigationHistory")
            index = history["currentIndex"] + (1 if where == "forward" else -1)
            entries = history["entries"]
            if index < 0 or index >= len(entries):
                raise ValueError(f"No history entry to go {where}")
            call("Page.navigateToHistoryEntry", entryId=entries[index]["id"])
        elif where.startswith(("http://", "https://")):
            # Going to an address is the one navigation that was missing, and
            # its absence bites exactly where a run is most fragile: reaching
            # the screen under test. Without it a case has to start at the home
            # page and click its way in, so every step of that approach is
            # another way to fail at something the case was not testing — and
            # when a session drops mid-run there is no way back to where it was.
            #
            # Only http and https. A `javascript:` or `data:` address would be
            # code execution wearing a URL, and this engine deliberately never
            # lets a target turn into code.
            result = call("Page.navigate", url=where)
            if result.get("errorText"):
                raise ValueError(f"Could not open {where}: {result['errorText']}")
        else:
            raise ValueError(
                "`where` must be reload, back, forward, or an http(s) address")
        return {"navigated": where}

    if operation == "highlight":
        # For the person watching. A run driving a window in front of someone
        # is hard to follow when nothing says where the next click landed.
        node = request.get("node")
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        shown = evaluate("""(node => {
          const e=window.__jevFast?.nodes.get(node);
          if (!e?.isConnected) return null;
          const r=e.getBoundingClientRect();
          const ring=document.createElement('div');
          ring.style.cssText=`position:fixed;left:${r.x-3}px;top:${r.y-3}px;
            width:${r.width+6}px;height:${r.height+6}px;border:2px solid #FFB547;
            border-radius:6px;pointer-events:none;z-index:2147483647;
            box-shadow:0 0 0 9999px rgba(0,0,0,.08)`;
          document.body.appendChild(ring);
          setTimeout(() => ring.remove(), 900);
          return {x:r.x, y:r.y};
        })(%s)""" % json.dumps(node))
        if shown is None:
            raise StalePage("That element is no longer in the document")
        return {"highlighted": node}

    if operation == "key":
        # Keys that are not text.
        #
        # Escape closes a dialog, Enter commits a cell, F2 opens the editor, the
        # arrows move the active cell. None of them can be typed: `insertText`
        # inserts characters, and a grid waiting for F2 receives nothing.
        #
        # This is also why "press Escape" looked unreliable rather than absent —
        # there was no way to send one, so whatever was tried was something
        # else, and the page ignored it exactly as it should.
        name, code, virtual = key_spec(request.get("key"))
        held = modifier_mask(request.get("modifiers"))
        pressed = []
        try:
            for mod_name, mod_code, bit in MODIFIERS:
                if held & bit:
                    call("Input.dispatchKeyEvent", type="rawKeyDown", key=mod_name,
                         code=mod_code, modifiers=held)
                    pressed.append((mod_name, mod_code))
            down = {"type": "rawKeyDown" if code else "keyDown", "key": name,
                    "windowsVirtualKeyCode": virtual, "nativeVirtualKeyCode": virtual,
                    "modifiers": held}
            if code:
                down["code"] = code
            else:
                # A printable key carries its text, or nothing is inserted.
                down["text"] = name
                down["unmodifiedText"] = name
            call("Input.dispatchKeyEvent", **down)
            up = {"type": "keyUp", "key": name, "windowsVirtualKeyCode": virtual,
                  "nativeVirtualKeyCode": virtual, "modifiers": held}
            if code:
                up["code"] = code
            call("Input.dispatchKeyEvent", **up)
        finally:
            for mod_name, mod_code in reversed(pressed):
                call("Input.dispatchKeyEvent", type="keyUp", key=mod_name, code=mod_code)
        return {"pressed": name}

    if operation == "component":
        # Some state has no attribute and no control.
        #
        # A custom element takes its rows, its tasks, its layout as JS
        # properties — arrays and objects with no attribute equivalent. There is
        # no gesture that sets them, so a run cannot arrange the state it wants
        # to test, and cannot read the state it should verify. The application
        # itself does this: it reads a designer's layout with getLayout().
        #
        # Names only. The property or method is named, the arguments are JSON;
        # nothing here evaluates an expression written by a model, so the same
        # rule holds as for node ids — the page never runs model text.
        node = request.get("node")
        member = request.get("member")
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        member = member_name(member)
        payload = {"node": node, "member": member, "mode": request.get("mode", "get"),
                   "args": request.get("args") or [], "value": request.get("value")}
        if payload["mode"] not in ("get", "set", "call"):
            raise ValueError("`mode` must be get, set or call")
        result = evaluate("""(req => {
          const e=window.__jevFast?.nodes.get(req.node);
          if (!e?.isConnected) return {missing:true};
          // Own prototype chain only: no reaching into globals through a name.
          if (!(req.member in e)) return {unknown:true};
          try {
            if (req.mode === 'set') { e[req.member] = req.value; return {ok:true}; }
            const value = req.mode === 'call' ? e[req.member](...req.args) : e[req.member];
            // Whatever comes back is data from the page. Serialised, capped,
            // never handed on as something to run.
            let shown;
            try { shown = JSON.stringify(value ?? null); }
            catch { shown = String(value).slice(0, 2000); }
            return {ok:true, value: shown === undefined ? null : String(shown).slice(0, 8000)};
          } catch (err) { return {failed: String(err?.message || err).slice(0, 300)}; }
        })(%s)""" % json.dumps(payload))
        if not result or result.get("missing"):
            raise StalePage("That element is no longer in the document")
        if result.get("unknown"):
            raise ValueError(f"The element has no member named {member!r}")
        if result.get("failed"):
            raise RuntimeError(f"{member} raised: {result['failed']}")
        return {"member": member, "value": result.get("value")}

    if operation == "listen":
        # Verification by effect, not by action.
        #
        # "The click did not raise an error" is the weakest claim a run can
        # make. Re-reading the DOM is better but still indirect: a board that
        # moved a card and a board that re-rendered for another reason look the
        # same from outside.
        #
        # Components say what happened. They emit CustomEvents — card-move,
        # task-change, render-complete, save-error — and the ones worth
        # listening to are `composed`, so they cross the shadow boundary and
        # arrive at the document. Recording them around an action turns "it
        # probably worked" into "the component says it did".
        names = request.get("events") or []
        if not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise ValueError("Pass `events` as a list of event names to record")
        # Names only reach addEventListener; nothing here evaluates model text.
        call("Runtime.evaluate", expression="""(names => {
          const box = window.__jevHeard ||= {seen:[], stop:[]};
          for (const off of box.stop) off();
          box.stop = []; box.seen = [];
          for (const name of names) {
            const handler = (event) => {
              // The detail is data from the page: recorded, never executed, and
              // capped so one chatty event cannot fill the observation.
              let detail = null;
              try { detail = JSON.stringify(event.detail ?? null)?.slice(0, 2000) ?? null; }
              catch { detail = '[unserialisable]'; }
              box.seen.push({name: event.type, detail, at: Math.round(performance.now())});
              if (box.seen.length > 50) box.seen.shift();
            };
            document.addEventListener(name, handler, true);
            box.stop.push(() => document.removeEventListener(name, handler, true));
          }
          return names.length;
        })(%s)""" % json.dumps([n.strip() for n in names]), returnByValue=True)
        return {"listening": [n.strip() for n in names]}

    if operation == "heard":
        # What arrived since `listen`. Empty is an answer: it means the gesture
        # reached the page and the component did not consider anything to have
        # happened — which is exactly the silent failure that re-reading the DOM
        # tends to miss.
        seen = evaluate("(() => (window.__jevHeard?.seen ?? []))()")
        if request.get("clear"):
            call("Runtime.evaluate", expression="(() => { if (window.__jevHeard) window.__jevHeard.seen = []; })()")
        return {"events": seen or []}

    if operation == "touch":
        # Touch or mouse is not a property of the run, it is a property of the
        # STEP. An application that branches on pointer type renders a different
        # tree for each: with touch on, an on-screen keypad appears and HTML5
        # drag disappears; with it off, the reverse. Choosing once per session
        # means half the product is untestable, so it is switchable.
        on = bool(request.get("enabled", True))
        call("Emulation.setTouchEmulationEnabled", enabled=on,
             maxTouchPoints=int(request.get("points", 1)) if on else 1)
        call("Emulation.setEmitTouchEventsForMouse", enabled=on,
             configuration="mobile" if on else "desktop")
        return {"touch": on}

    if operation == "hover":
        # Hovering is not standing still. A tooltip that listens for mousemove
        # never fires if the pointer is teleported to its centre, and a control
        # revealed at `opacity: 0` stays invisible to anything reading pixels —
        # while being perfectly clickable, which is how a run ends up reporting
        # that it clicked something nobody can see.
        node = request.get("node")
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        spot = evaluate("""(node => {
          const e=window.__jevFast?.nodes.get(node);
          if (!e?.isConnected) return null;
          const r=e.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          return {x:r.x+r.width/2, y:r.y+r.height/2};
        })(%s)""" % json.dumps(node))
        if spot is None:
            raise StalePage("That element is gone or has no box to hover")
        # Approach from slightly off, then settle: two real moves, because one
        # event at the destination is indistinguishable from never having moved.
        for x, y in ((spot["x"] - 12, spot["y"] - 12), (spot["x"], spot["y"])):
            call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            time.sleep(0.03)
        time.sleep(float(request.get("settle", 0.25)))
        return {"hovered": node}

    if operation in ("drag", "contextmenu"):
        # Gestures a menu of actions cannot offer.
        #
        # Dragging needs an origin AND a destination: offering one action per
        # pair would be the cartesian product of everything on screen. So these
        # are asked for by name, by a caller that knows what it wants to move —
        # not chosen from a list.
        #
        # Both resolve node ids the observation already found, so the same rule
        # holds as everywhere else: the page never sees a selector written by a
        # model.
        def centre(node, what):
            if type(node) is not int:
                raise ValueError(f"Invalid observed node for {what}")
            spot = evaluate("""(node => {
              const e=window.__jevFast?.nodes.get(node);
              if (!e?.isConnected || !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
                return null;
              const r=e.getBoundingClientRect();
              // `display: contents` generates no box: the element is in the
              // tree and has no geometry, so there is nothing to aim at. Its
              // children are the real targets.
              if (!r.width || !r.height) return null;
              return {x:r.x+r.width/2, y:r.y+r.height/2};
            })(%s)""" % json.dumps(node))
            if spot is None:
                raise StalePage(f"The {what} is gone, hidden, or has no box to aim at")
            return spot["x"], spot["y"]

        if operation == "contextmenu":
            x, y = centre(request.get("node"), "target")
            call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                 button="right", clickCount=1)
            call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                 button="right", clickCount=1)
            return {"executed": "contextmenu"}

        # HTML5 drag and drop is a different machine from mouse events. An
        # element with `draggable` listens for dragstart/dragover/drop carrying
        # a DataTransfer; press-move-release never produces one, so a board
        # built on it does not move a single card no matter how well aimed the
        # mouse path is. The events have to be synthesised, sharing one
        # DataTransfer so what the source writes is what the target reads.
        if request.get("mode") == "html5":
            if request.get("to") is None:
                raise ValueError("An HTML5 drag needs a destination element: pass `to`")
            moved = evaluate("""(req => {
              const from=window.__jevFast?.nodes.get(req.node);
              const to=window.__jevFast?.nodes.get(req.to);
              if (!from?.isConnected || !to?.isConnected) return null;
              const data=new DataTransfer();
              const fire=(el,type,extra)=>{
                const r=el.getBoundingClientRect();
                const ev=new DragEvent(type,{bubbles:true,cancelable:true,composed:true,
                  dataTransfer:data,
                  clientX:r.x+r.width/2, clientY:r.y+(extra?.atTop ? 2 : r.height/2)});
                el.dispatchEvent(ev);
                return ev;
              };
              fire(from,'dragstart');
              fire(to,'dragenter');
              // The insertion index usually comes from the LAST dragover, so
              // the one that counts is the one just before the drop.
              fire(to,'dragover',{atTop:req.atTop});
              const dropped=fire(to,'drop',{atTop:req.atTop});
              fire(from,'dragend');
              return {dropped:dropped.defaultPrevented};
            })(%s)""" % json.dumps({k: request.get(k) for k in ("node", "to", "atTop")}))
            if moved is None:
                raise StalePage("One end of the drag is no longer in the document")
            # A handler that accepts a drop calls preventDefault. Without it the
            # events fired and nobody listened, which is not a move.
            if not moved["dropped"]:
                raise RuntimeError("Nothing accepted the drop; the target may not be a drop zone.")
            return {"executed": "drag", "mode": "html5"}

        start = centre(request.get("node"), "drag origin")
        if request.get("to") is not None:
            end = centre(request["to"], "drag destination")
        else:
            end = (start[0] + float(request.get("dx", 0)), start[1] + float(request.get("dy", 0)))
        if end == start:
            raise ValueError("A drag needs a destination: pass `to`, or dx/dy")

        # Intermediate points are not padding. A timeline computes the new date
        # from the delta of each move, a board decides the insertion index from
        # the LAST dragover, and a drag with no movement between press and
        # release is read as a click. Steps make it a drag.
        steps = max(2, int(request.get("steps", 8)))
        path = [(start[0] + (end[0] - start[0]) * i / steps,
                 start[1] + (end[1] - start[1]) * i / steps) for i in range(1, steps + 1)]

        call("Input.dispatchMouseEvent", type="mousePressed", x=start[0], y=start[1],
             button="left", clickCount=1)
        try:
            for x, y in path:
                call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="left")
                # A component that captures the pointer and recalculates on each
                # move needs time to do it; firing the whole path in one tick
                # lands every event on the same frame.
                time.sleep(0.02)
        finally:
            call("Input.dispatchMouseEvent", type="mouseReleased", x=path[-1][0], y=path[-1][1],
                 button="left", clickCount=1)
        return {"executed": "drag", "from": list(start), "to": list(path[-1])}

    if operation == "hold":
        # Some behaviour exists only while a key is DOWN: Alt revealing the code
        # under a name, Ctrl showing the shortcuts of the screen. A press and a
        # release with nothing in between observes the page as it was, so the
        # feature reads as absent and the run reports a failure that is its own.
        #
        # Press, observe, release — one operation. Splitting it into two calls
        # would leave the key held between them, and any later step would run
        # inside a modifier nobody remembers pressing.
        held = modifier_mask(request.get("modifiers"))
        if not held:
            raise ValueError("Nothing to hold; pass modifiers such as ['alt']")
        pressed = []
        try:
            for name, code, bit in MODIFIERS:
                if held & bit:
                    call("Input.dispatchKeyEvent", type="rawKeyDown", key=name,
                         code=code, modifiers=held)
                    pressed.append((name, code))
            # Some of these appear after a deliberate delay — the shortcut hint
            # waits 450 ms so it does not flash on every copy-paste. Observing
            # immediately would miss exactly what we came to see.
            time.sleep(float(request.get("settle", 0.8)))
            seen = evaluate(READ_STATE)
        finally:
            for name, code in reversed(pressed):
                call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code)
        if seen is None:
            raise StalePage("The document changed while the key was held")
        return seen

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            node = action.get("node")
            if node is None:
                # Page scroll. The fixed point is arbitrary but harmless: with
                # nothing scrollable under it the wheel falls through to the
                # document, which is what this branch means.
                call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650,
                     deltaX=0, deltaY=action["delta"])
            else:
                if type(node) is not int:
                    raise ValueError("Invalid observed node")
                # Aimed at the container the observation found, not at a guessed
                # point. A wheel event at a fixed coordinate scrolls whatever
                # happens to sit there — on a page that does not scroll, often
                # nothing at all, and the agent reads that as "the list ends
                # here". Setting scrollTop asks the element directly, and the
                # return value says whether it actually moved, so a container
                # already at its end cannot be mistaken for a working scroll.
                moved = evaluate("""(action => {
                  const e=window.__jevFast?.nodes.get(action.node);
                  if (!e?.isConnected || !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
                    return null;
                  const sideways=action.axis==='x';
                  const before=sideways ? e.scrollLeft : e.scrollTop;
                  if (sideways) e.scrollLeft=before+action.delta;
                  else e.scrollTop=before+action.delta;
                  const after=sideways ? e.scrollLeft : e.scrollTop;
                  return {moved:after!==before,before,after};
                })(%s)""" % json.dumps({k: action.get(k) for k in ("node", "delta", "axis")}))
                if moved is None:
                    raise StalePage("The container is gone or hidden")
                if not moved["moved"]:
                    raise RuntimeError("The container did not move; it is already at that end.")
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            # Every refusal below says WHICH refusal it is.
            #
            # They all used to come back as one null, and one null became
            # "Target changed or is covered" — a sentence that names two very
            # different situations and is wrong about at least one of them
            # every time. The run then reports a stale page when what actually
            # happened was a cookie banner on top of the button, or a field
            # that went read-only, or a control that scrolled out of view.
            #
            # Each of those has a different next step, and telling them apart
            # is most of the difference between "the application failed" and
            # "the driver failed".
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected) return {no:'gone from the document'};
              if (e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]'))
                return {no:'disabled now — something earlier in the form turned it off'};
              if (!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
                return {no:'no longer visible'};
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true'))
                return {no:'read-only now'};
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height) return {no:'has no size on screen'};
              if (x<0 || y<0 || x>=innerWidth || y>=innerHeight)
                return {no:'scrolled out of the viewport — scroll to it first'};
              // elementFromPoint stops at the shadow boundary and returns the
              // component HOST, so `e.contains(hit)` is false for anything
              // inside it and the action is discarded as unreachable. The agent
              // then picks the same control again, forever: that is the loop.
              // Descending through open roots asks the real question — is the
              // thing under the cursor this element, or inside it?
              const deepHit=(px,py)=>{
                let node=document.elementFromPoint(px,py);
                while (node?.shadowRoot) {
                  const inner=node.shadowRoot.elementFromPoint(px,py);
                  if (!inner || inner===node) break;
                  node=inner;
                }
                return node;
              };
              const hit=deepHit(x,y);
              if (!(hit===e || e.contains(hit) || hit?.contains(e))) {
                // Name what is on top. "Covered" sends a run looking for a
                // problem in the control; "covered by the cookie banner"
                // sends it to close the banner, which is the actual step.
                const who=hit ? (hit.getAttribute?.('aria-label') || hit.id ||
                                 (hit.innerText||'').trim().split(String.fromCharCode(10))[0] ||
                                 hit.className || hit.tagName || '').toString().slice(0,60) : '';
                return {no: who ? `covered by "${who}"` : 'covered by something else'};
              }
              if (action.kind==='select') {
                if (e.tagName!=='SELECT') return {no:'is not a dropdown'};
                if (![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]')))
                  return {no:'has no such option available'};
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            refused = (target or {}).get("no") if isinstance(target, dict) else None
            if target is None or refused:
                because = refused or "changed while it was being resolved"
                if kind == "select":
                    raise RuntimeError(
                        f"Dropdown execution was not confirmed: it {because}. "
                        "Inspect before retrying.")
                raise StalePage(f"Cannot act: the target {because}. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                # Holding a modifier is a gesture, not decoration. Applications
                # hang real behaviour on it: Alt to reveal the code under a
                # name, Ctrl to show the shortcuts of the screen, Shift to
                # extend a selection, Ctrl-click to open in a new tab. Without
                # this the agent cannot reach any of it — and cannot verify a
                # feature whose whole point is the key being down.
                #
                # The modifier is pressed, the click happens inside it, and it
                # is released in a `finally`: leaving Ctrl stuck down poisons
                # every later step of the run, and that failure looks like the
                # page misbehaving rather than the driver.
                held = modifier_mask(action.get("modifiers"))
                pressed = []
                try:
                    for name, code, bit in MODIFIERS:
                        if held & bit:
                            call("Input.dispatchKeyEvent", type="rawKeyDown", key=name,
                                 code=code, modifiers=held)
                            pressed.append((name, code))
                    # A grid that edits in place opens its editor on the SECOND
                    # click. Sending one leaves the cell exactly as it was, so
                    # the whole capture flow of a document is undrivable.
                    # Chrome wants the full sequence with a rising count, not a
                    # single event with clickCount 2.
                    if kind == "dblclick":
                        # The gap matters. Some screens implement "double click"
                        # themselves, counting two ordinary clicks inside a
                        # window of their own choosing — 400 ms here, 450 ms
                        # there. Too slow and it is two single clicks; too fast
                        # and a native handler may coalesce them.
                        gap = float(request.get("interval", 80)) / 1000.0
                        for count in (1, 2):
                            for event in ("mousePressed", "mouseReleased"):
                                call("Input.dispatchMouseEvent", type=event, x=x, y=y,
                                     button="left", clickCount=count, modifiers=held)
                            if count == 1:
                                time.sleep(gap)
                    else:
                        call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                             button="left", clickCount=1, modifiers=held)
                        # A long press is a gesture in its own right on touch:
                        # context menus and multi-select hang off it, and a
                        # press with no duration never reaches them.
                        hold_ms = float(request.get("press", 0) or 0)
                        if hold_ms > 0:
                            time.sleep(hold_ms / 1000.0)
                        call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                             button="left", clickCount=1, modifiers=held)
                finally:
                    for name, code in reversed(pressed):
                        call("Input.dispatchKeyEvent", type="keyUp", key=name, code=code)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    # `insertText` lands the whole string in one go. That is the
                    # right default — it is fast and it reaches contenteditable
                    # editors that only listen for beforeinput.
                    #
                    # But rhythm is sometimes part of the meaning. A point of
                    # sale watches the gap between keystrokes to tell a barcode
                    # reader from a person typing, and a search box with a
                    # debounce only fires once the typing stops. Neither can be
                    # exercised by a string that appears all at once, and
                    # neither failure looks like a timing problem: one adds a
                    # line nobody asked for, the other returns the results of
                    # the previous query.
                    per_key = float(request.get("key_delay", 0) or 0)
                    if per_key > 0:
                        for character in request["text"]:
                            call("Input.dispatchKeyEvent", type="keyDown", text=character,
                                 key=character, unmodifiedText=character)
                            call("Input.dispatchKeyEvent", type="keyUp", key=character)
                            time.sleep(per_key / 1000.0)
                    else:
                        call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
