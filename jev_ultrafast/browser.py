"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
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
        """
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

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
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
              if (!e.contains(document.elementFromPoint(x,y))) return null;
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
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
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
