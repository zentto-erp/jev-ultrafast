"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

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


def read_state_script():
    """The observation, with this run's budgets attached."""
    source = Path(__file__).with_name("snapshot.js").read_text()
    return f"(() => {{ window.__jevBudgets={json.dumps(snapshot_budgets())}; return {source}; }})()"


# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = read_state_script()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url, *, reuse_target=None, viewport="fixed"):
        ensure_daemon()
        # A fresh background tab per run is right for benchmarks: runs stay
        # isolated and the user's Chrome never steals focus. It is wrong when a
        # person is watching a sequence of runs, because every run opens another
        # tab and whatever they were looking at is no longer where the work
        # happens. `reuse_target` drives the existing tab instead.
        self.target = reuse_target or cdp(
            "Target.createTarget", url="about:blank", background=True
        )["targetId"]
        self.owns_target = reuse_target is None
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
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

    def observe(self, screenshot=True):
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
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

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

    def close(self):
        # A borrowed tab is not ours to close: the caller is reusing it across
        # runs, and closing it would defeat the reason for lending it.
        if self.target and self.owns_target:
            cdp("Target.closeTarget", targetId=self.target)
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
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
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
              if (!(hit===e || e.contains(hit) || hit?.contains(e))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
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
                        for count in (1, 2):
                            for event in ("mousePressed", "mouseReleased"):
                                call("Input.dispatchMouseEvent", type=event, x=x, y=y,
                                     button="left", clickCount=count, modifiers=held)
                    else:
                        for event in ("mousePressed", "mouseReleased"):
                            call("Input.dispatchMouseEvent", type=event, x=x, y=y,
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
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
