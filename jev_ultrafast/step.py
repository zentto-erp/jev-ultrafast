"""One directed step against an already-open tab.

The main loop asks a model what to do next. That is right when the next move is
a judgement, and wrong when the caller already knows: a test case naming the row
to drag, the value to read, the key to hold. Asking a model to guess a move you
have written down is slower, costs more, and can pick something else.

This is the other door. Every operation resolves an element the OBSERVATION
found — by its id, or by matching the label the observation gave it — so the
page never sees a selector written by a model, here either.

    python -m jev_ultrafast.step --reuse-tab <id> observe
    python -m jev_ultrafast.step --reuse-tab <id> click --match "Nueva Compra"
    python -m jev_ultrafast.step --reuse-tab <id> inspect --match "Total"
    python -m jev_ultrafast.step --reuse-tab <id> listen --events card-move,save
    python -m jev_ultrafast.step --reuse-tab <id> heard

Output is JSON on stdout, so a case can be a shell script and still assert.
"""

import argparse
import json
import sys

from .browser import Browser, StalePage


def find(page, text, kind=None):
    """The action whose label matches, preferring an exact hit.

    Matching on the label the observation produced — not on a CSS selector — is
    what keeps the two doors consistent: both operate on things that were
    actually seen.
    """
    wanted = (text or "").strip().lower()
    if not wanted:
        raise SystemExit("--match needs text")
    candidates = [a for a in page["actions"] if kind in (None, a.get("kind"))]
    exact = [a for a in candidates if (a.get("label") or "").strip().lower() == wanted]
    partial = [a for a in candidates if wanted in (a.get("label") or "").lower()]
    hits = exact or partial
    if not hits:
        # The control may be perfectly usable and simply not clickable from
        # here: scrolled out of the viewport, or behind a toolbar that collapses
        # at this width. If the screen publishes a key for it, that key works
        # regardless — it needs no rectangle — so saying so turns a dead end
        # into the next step instead of a failed run.
        shortcut = [k for k in page.get("keys", [])
                    if wanted in (k.get("label") or "").lower() and not k.get("disabled")]
        if shortcut:
            raise SystemExit(json.dumps({
                "error": "not_clickable",
                "message": f"{shortcut[0]['label']!r} is not reachable as a control here, "
                           f"but the screen binds {shortcut[0]['key']} to it — press the key instead",
                "key": shortcut[0]["key"],
            }))
        blocked = [b["label"] for b in page.get("blocked", [])
                   if wanted in (b.get("label") or "").lower()]
        if blocked:
            # The commonest dead end in a dialog: the button exists and is
            # waiting for something else to happen first.
            raise SystemExit(json.dumps({
                "error": "disabled",
                "message": f"{blocked[0]!r} is present but disabled — do the step that enables it first",
            }))
        raise SystemExit(json.dumps({
            "error": "not_found",
            "looked_for": text,
            "available": [a.get("label") for a in candidates][:40],
        }))
    return hits[0]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jev-step", description=__doc__)
    parser.add_argument("operation", choices=[
        "observe", "keys", "find", "click", "dblclick", "fill", "inspect", "listen", "heard",
        "component", "hold", "hover", "drag", "contextmenu", "touch", "scroll", "key",
        "arm", "console", "network", "state", "sealed", "declared", "upload", "navigate", "highlight",
    ])
    parser.add_argument("--reuse-tab", required=True, help="targetId of the open tab")
    parser.add_argument("--match", help="text of the control, as the observation labelled it")
    parser.add_argument("--to", help="text of the destination control, for drag")
    parser.add_argument("--text", help="what to type")
    parser.add_argument("--key-delay", type=float, default=0,
                        help="ms between keystrokes; a point of sale reads the rhythm")
    parser.add_argument("--events", help="comma-separated event names")
    parser.add_argument("--member", help="property or method name")
    parser.add_argument("--mode", default="get",
                        choices=["get", "set", "call", "html5", "pointer", "accept", "dismiss",
                                 "all", "save", "load"])
    parser.add_argument("--value", help="JSON value for component set")
    parser.add_argument("--modifiers", help="comma-separated: alt, ctrl, meta, shift")
    parser.add_argument("--press", type=float, default=0, help="ms to hold a click down")
    parser.add_argument("--enabled", default="true", choices=["true", "false"])
    parser.add_argument("--viewport", default="none",
                        help="'none' keeps the window the owner set — the default here on purpose")
    args = parser.parse_args(argv)

    browser = Browser(None, reuse_target=args.reuse_tab, viewport=args.viewport)
    try:
        if args.operation == "touch":
            return browser.touch(enabled=args.enabled == "true")
        if args.operation == "listen":
            return browser.listen([e.strip() for e in (args.events or "").split(",") if e.strip()])
        if args.operation == "heard":
            return browser.heard(clear=True)
        if args.operation == "arm":
            return browser.arm(dialogs=args.mode if args.mode in ("accept", "dismiss") else "accept",
                               text=args.text or "")
        if args.operation == "console":
            return browser.console(clear=True)
        if args.operation == "network":
            # Only the failures by default: a screen can make forty calls in a
            # step, and listing all of them buries the one that matters.
            return browser.network(all_calls=args.mode == "all", clear=False)
        if args.operation == "state":
            if not args.text:
                raise SystemExit("state needs --text <file>")
            return browser.state("load" if args.mode == "load" else "save", args.text)
        if args.operation == "declared":
            # Lo que la pagina dice saber hacer, y llamar una de esas cosas.
            return browser.declared(args.match, args.value)
        if args.operation == "sealed":
            # Closed components: list what is inside, or press one.
            return browser.sealed(args.match)
        if args.operation == "navigate":
            # `--text` carries the destination: reload, back, forward, or a URL.
            return browser.navigate(args.text or "reload")
        if args.operation == "hold":
            return browser.hold([m.strip() for m in (args.modifiers or "").split(",") if m.strip()])
        if args.operation == "key":
            # Not aimed at an element: it goes wherever the focus is, which is
            # what Escape and Enter mean. A case that needs it somewhere
            # specific clicks there first.
            return browser.key(args.text or args.match,
                               [m.strip() for m in (args.modifiers or "").split(",") if m.strip()])

        page = browser.observe(screenshot=False)
        if args.operation == "observe":
            # Trimmed on purpose: the whole observation is large and a case
            # needs to see what it can reach, not every byte of it.
            return {
                "url": page["url"],
                "actions": [{"id": a["id"], "kind": a["kind"], "label": a.get("label")}
                            for a in page["actions"]],
                "blocked": page.get("blocked", []),
                "keys": page.get("keys", []),
                "omitted_actions": page.get("omitted_actions", 0),
            }
        if args.operation == "keys":
            return {"url": page["url"], "keys": page.get("keys", [])}
        if args.operation == "find":
            # Look for one thing instead of reading the whole screen.
            #
            # An observation of a dense screen is a few hundred actions and
            # thousands of characters of text, and a step that only needs to
            # know "is there a Confirm button, and can I press it" pays for all
            # of it. Worse, it pays again on every later turn, because the
            # answer stays in the conversation — so one careless observation on
            # a long run is charged hundreds of times over.
            #
            # This asks the question and returns the answer.
            if not args.match:
                raise SystemExit("find needs --match <text>")
            wanted = args.match.strip().lower()
            hits = [{"id": a["id"], "kind": a["kind"], "label": a.get("label")}
                    for a in page["actions"] if wanted in (a.get("label") or "").lower()]
            # The three places a control can be, not just the reachable one. A
            # `find` that answers "no" because the button is disabled, or
            # because it is only a key, teaches the run the wrong lesson.
            stopped = [b for b in page.get("blocked", [])
                       if wanted in (b.get("label") or "").lower()]
            keys = [k for k in page.get("keys", [])
                    if wanted in (k.get("label") or "").lower()]
            # And the page's own words, for what is read rather than pressed:
            # a total, an error, a status. One line either side is enough to
            # tell "Saldo 0,00" from "Saldo" as a column heading.
            lines = (page.get("text") or "").split("\n")
            around = []
            for at, line in enumerate(lines):
                if wanted in line.lower():
                    around.append("\n".join(lines[max(0, at - 1):at + 2]))
                    if len(around) >= 5:
                        break
            return {"url": page["url"], "looked_for": args.match,
                    "actions": hits, "blocked": stopped, "keys": keys, "text": around,
                    "found": bool(hits or stopped or keys or around)}

        if args.operation == "scroll":
            target = find(page, args.match, kind="scroll")
            return browser.act(target, page)

        target = find(page, args.match)
        node = target["node"]

        if args.operation == "inspect":
            return browser.inspect(node)
        if args.operation == "hover":
            return browser.hover(node)
        if args.operation == "highlight":
            return browser.highlight(node)
        if args.operation == "upload":
            return browser.upload(node, [f.strip() for f in (args.text or "").split("|") if f.strip()])
        if args.operation == "contextmenu":
            return browser.context_menu(node)
        if args.operation == "component":
            value = json.loads(args.value) if args.value else None
            return browser.component(node, args.member, mode=args.mode, value=value)
        if args.operation == "drag":
            destination = find(page, args.to)["node"] if args.to else None
            return browser.drag(node, to=destination,
                                mode="html5" if args.mode == "html5" else None)
        if args.operation == "fill":
            action = dict(target, kind="fill")
            return browser_act_with(browser, action, page, args.text, args.key_delay)
        action = dict(target, kind="dblclick" if args.operation == "dblclick" else "click")
        if args.modifiers:
            action["modifiers"] = [m.strip() for m in args.modifiers.split(",") if m.strip()]
        if args.press:
            action["press"] = args.press
        return browser.act(action, page)
    finally:
        # A borrowed tab stays open: the owner is watching it.
        browser.close()


def browser_act_with(browser, action, page, text, key_delay):
    from .browser import browser_operation
    if not browser.fresh(page, action):
        raise StalePage("Page changed since this decision. Observe again.")
    return browser_operation({"operation": "act", "session": browser.session,
                              "action": action, "text": text or "",
                              "key_delay": key_delay})


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except SystemExit as stop:
        print(stop.code if isinstance(stop.code, str) else json.dumps({"error": str(stop.code)}))
        sys.exit(1)
    except Exception as failure:
        print(json.dumps({"error": type(failure).__name__, "message": str(failure)}))
        sys.exit(1)
