"""Local-browser freshness/execution regressions. No model calls or external websites."""

from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


# Una pantalla con mas controles de los que caben en el tope, y un autocompletado
# encima. Es la unica forma de reproducir el fallo: en una pagina pequena las
# sugerencias caben siempre y todo parece correcto.
CROWDED = """<!doctype html><title>Crowded screen</title>
<style>#portal{position:absolute;background:#fff;border:1px solid #ccc}
#portal [role=option]{padding:6px}</style>
<label>Sector<input id="sector" role="combobox" aria-expanded="false"
  aria-controls="portal" aria-autocomplete="list" autocomplete="off"></label>
<div id="filler"></div>
<!-- El portal va DESPUES de los controles, que es donde lo inyecta una
     aplicacion real: al final del body, por encima de todo. Ponerlo antes hacia
     que sus opciones se recogieran primero y sobrevivieran al tope sin
     esfuerzo — el guard pasaba con y sin el arreglo, y por tanto no probaba
     nada. El orden del DOM ES el caso. -->
<div id="portal" role="listbox" hidden></div>
<script>
  const OPTIONS=["Software","Software de contabilidad","Servicios financieros"];
  const field=document.querySelector('#sector'), portal=document.querySelector('#portal');
  // Mas controles que el tope, para que las sugerencias tengan que competir.
  document.querySelector('#filler').innerHTML=
    Array.from({length:300},(_,i)=>'<button>Row action '+i+'</button>').join('');
  field.addEventListener('input',()=>{
    const hit=OPTIONS.filter(o=>o.toLowerCase().includes(field.value.toLowerCase()));
    if (!field.value || !hit.length) { portal.hidden=true; field.setAttribute('aria-expanded','false'); return; }
    const box=field.getBoundingClientRect();
    portal.style.left=(box.left+scrollX)+'px';
    portal.style.top=(box.bottom+scrollY)+'px';
    portal.innerHTML=hit.map(o=>'<div role=option>'+o+'</div>').join('');
    portal.hidden=false;
    field.setAttribute('aria-expanded','true');
  });
  portal.addEventListener('click',e=>{
    const chosen=e.target.closest('[role=option]');
    if (!chosen) return;
    field.value=chosen.textContent;
    portal.hidden=true;
    field.setAttribute('aria-expanded','false');
  });
</script>"""


def crowded_screen_keeps_the_suggestions():
    """El tope no se puede comer el unico paso que continua lo escrito.

    Un autocompletado se pinta en un portal al final del documento, asi que sus
    opciones son las ultimas en recogerse — y en una pantalla con mas controles
    que el tope son exactamente lo que se descarta. El sintoma no parece un tope:
    el campo queda escrito, no hay ninguna sugerencia ofrecida y el recorrido se
    para diciendo que no puede seguir, a un clic del final. Fue el fallo de las
    aptitudes y el sector de LinkedIn, que nunca tuvo que ver con no saber elegir
    una sugerencia.
    """
    browser = Browser("data:text/html," + quote(CROWDED))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        assert page["omitted_actions"] > 0, "la pantalla tiene que agotar el tope para probar esto"
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Software")

        page = browser.observe(screenshot=False)
        options = [a for a in page["actions"] if a.get("role") == "option"]
        assert options, "con el tope agotado, las sugerencias tienen que seguir ofreciendose"
        passed.append(f"a crowded screen still offers its {len(options)} live suggestions")

        browser.act(options[0], page)
        chosen = browser.evaluate("document.querySelector('#sector').value")
        assert chosen == "Software", repr(chosen)
        passed.append("the offered suggestion is the one the page applies")
    finally:
        browser.close()
    return passed


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")
        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")
    finally:
        browser.close()
    print("\n".join(passed))
    passed += crowded_screen_keeps_the_suggestions()
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
