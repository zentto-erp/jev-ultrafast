(() => {
  if (!document.body) return null;
  // Budgets. The defaults are the historical ones and stay put; a dense
  // enterprise table can exhaust them long before the page is exhausted, and
  // when that happens the run reports a screenful as if it were everything.
  // `omitted_actions` says how many were dropped — the text simply ends, which
  // is why raising it has to be possible without editing this file.
  const budget=(name,fallback)=>{
    const raw=window.__jevBudgets?.[name];
    const value=Number(raw);
    return Number.isFinite(value) && value>0 ? Math.floor(value) : fallback;
  };
  const MAX_ACTIONS=budget('actions',250);
  const MAX_TEXT=budget('text',6000);
  const MAX_SCROLLERS=budget('scrollers',4);
  const MAX_BLOCKED=budget('blocked',12);
  const MAX_KEYS=budget('keys',16);
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    crossRoots('input,textarea,select').filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=closestDeep(e,'form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Web components keep their content in a shadow root, and
  // document.querySelectorAll does not cross that boundary. On an app built
  // from them the agent sees an empty page: measured on a data grid, 0 rows
  // visible where the DOM had 13. It cannot click what it cannot see.
  const crossRootsFrom=(start,selector)=>{
    const found=[], seen=new Set();
    const walk=(root)=>{
      if (!root || seen.has(root)) return;
      seen.add(root);
      for (const e of root.querySelectorAll(selector)) found.push(e);
      // Only open roots are reachable; a closed one is deliberately private.
      for (const e of root.querySelectorAll('*')) if (e.shadowRoot) walk(e.shadowRoot);
      // A same-origin iframe is a document too, and its contents are as real as
      // anything else on the page — report viewers and print previews live
      // there. Cross-origin frames throw on access by design; that is a wall,
      // not a bug, so it is stepped over quietly.
      for (const f of root.querySelectorAll('iframe,frame')) {
        let inner=null;
        try { inner=f.contentDocument; } catch { inner=null; }
        if (inner) walk(inner);
      }
    };
    walk(start);
    return found;
  };
  // ── Acotar la observacion ─────────────────────────────────────────────
  //
  // Una pantalla de ERP tiene cabecera, menu lateral, pestanas del modulo y la
  // rejilla. Cuando el trabajo esta en la rejilla, los otros doscientos
  // controles no son contexto: son opciones que no pueden ser correctas,
  // compitiendo por el tope y por la atencion de quien decide. Acotar no es una
  // optimizacion de tamano, es quitarlas de la mesa.
  const SCOPE=typeof window.__jevScope==='string' && window.__jevScope.trim()
    ? window.__jevScope.trim() : null;
  const IGNORE=(Array.isArray(window.__jevIgnore) ? window.__jevIgnore : [])
    .filter(s=>typeof s==='string' && s.trim()).map(s=>s.trim());
  const IGNORE_SELECTOR=IGNORE.join(',');
  let scopeNode=null;
  if (SCOPE) { try { scopeNode=crossRootsFrom(document,SCOPE)[0] || null; } catch { scopeNode=null; } }
  // Pedir una parte y recibir la pantalla entera EN SILENCIO es peor que no
  // poder acotar: el recorrido decide sobre algo distinto de lo que cree, y no
  // hay nada en la salida que lo delate. Si el contenedor no esta se observa
  // todo, y se dice.
  const scope_missing=!!SCOPE && !scopeNode;
  const scopeRoot=scopeNode || document;
  const crossRoots=(selector)=>{
    const found=crossRootsFrom(scopeRoot,selector);
    return IGNORE_SELECTOR ? found.filter(e=>{
      try { return !closestDeep(e,IGNORE_SELECTOR); } catch { return true; }
    }) : found;
  };
  // Half of an application is not built from buttons. A card, a row, a tile or
  // a chip is a div with a click handler: no role, no tabindex, nothing the
  // standard selector matches — so it does not exist for the agent, which then
  // reports that the list cannot be opened. Measured on a data grid whose
  // mobile view is cards: every card a plain div, none of them reachable.
  //
  // Listeners cannot be read from script, so the honest proxy is the one the
  // author already gave the user: `cursor: pointer` means "this reacts". It is
  // also what accessibility tooling uses.
  const looksClickable=e=>{
    if (e.matches(selector)) return false;          // already offered
    if (e.closest('label')) return false;           // the control it labels is offered
    const style=getComputedStyle(e);
    const pointer=style.cursor==='pointer';
    const declared=e.hasAttribute('onclick') || e.hasAttribute('tabindex') ||
      ['row','listitem','treeitem','article'].includes(e.getAttribute('role'));
    if (!pointer && !declared) return false;
    // A parent painted with `pointer` makes every child look clickable. Offer
    // the OUTERMOST element of such a group: clicking a card is the intent,
    // clicking the text inside it is the same click reported four times.
    const parent=e.parentElement;
    if (parent && getComputedStyle(parent).cursor==='pointer' && !parent.matches(selector)) return false;
    // A container holding its own controls is scaffolding, not a target — the
    // controls inside are already offered and are the real intent.
    if (e.querySelector(selector)) return false;
    return true;
  };
  // `closest` stops at the shadow boundary and returns null, so a control
  // rendered inside a nested component never finds the row it belongs to. On an
  // app built from components that is most rows, and the label falls back to
  // the bare role — every control in the table reading the same again, which is
  // exactly what the row labelling below exists to prevent.
  // Walking up through `getRootNode().host` asks the question the author meant.
  const closestDeep=(e,sel)=>{
    let node=e;
    while (node) {
      const hit=node.closest?.(sel);
      if (hit) return hit;
      const root=node.getRootNode?.();
      node=root instanceof ShadowRoot ? root.host : null;
    }
    return null;
  };
  // A layout that fills the viewport never scrolls the page, so the page-level
  // scroll action below is not offered — while a grid inside it holds a hundred
  // rows behind its own scrollbar. Every control past the fold is dropped for
  // being off-screen, and the agent reports one screenful as if it were all
  // there is. These are the containers that actually scroll.
  const scrollables=()=>{
    const found=[];
    for (const e of crossRoots('*')) {
      const style=getComputedStyle(e);
      const downwards=/(auto|scroll)/.test(style.overflowY) && e.scrollHeight>e.clientHeight+2;
      const sideways=/(auto|scroll)/.test(style.overflowX) && e.scrollWidth>e.clientWidth+2;
      if ((!downwards && !sideways) || !visible(e)) continue;
      const r=e.getBoundingClientRect();
      // A container off-screen or thinner than a scrollbar cannot be aimed at.
      if (r.width<40 || r.height<40 || r.bottom<=0 || r.top>=innerHeight) continue;
      found.push({e,r,downwards,sideways});
    }
    // Innermost first: scrolling the outer shell when the grid is what holds
    // the rows moves the wrong thing, and the agent concludes nothing happened.
    return found.sort((a,b)=>(a.r.width*a.r.height)-(b.r.width*b.r.height)).slice(0,MAX_SCROLLERS);
  };
  const textRoots=()=>{
    // El texto tambien se acota: leer la cabecera y el menu cuando se pidio la
    // rejilla es la misma confusion que ofrecer sus botones.
    const roots=[scopeNode || document.body], seen=new Set();
    const walk=(root)=>{
      if (!root || seen.has(root)) return;
      seen.add(root);
      for (const e of root.querySelectorAll('*')) if (e.shadowRoot) { roots.push(e.shadowRoot); walk(e.shadowRoot); }
      // Text inside a same-origin frame is page text; a report preview that
      // renders there would otherwise read as a blank screen.
      for (const f of root.querySelectorAll('iframe,frame')) {
        let inner=null;
        try { inner=f.contentDocument; } catch { inner=null; }
        if (inner?.body) { roots.push(inner.body); walk(inner); }
      }
    };
    walk(scopeRoot);
    return roots;
  };
  const actions=[];
  // A control that is visible but disabled is not a dead end: it is the page
  // saying "do something else first". The confirm button of a picker dialog is
  // disabled until a row is ticked, so an agent that cannot see it concludes
  // there is no way to confirm — and takes Cancel, or Create new, which is
  // worse. Dropped from `actions` because it cannot be pressed, reported in
  // `blocked` because knowing it exists is what makes the next step obvious.
  const blocked=[];
  // And WHY, which is the part nobody reports.
  //
  // Knowing a control is disabled turns a dead end into "do something else
  // first". Knowing WHICH something else turns it into the next step. Without
  // it the agent still has to guess, and on a form with eight fields it will
  // guess wrong most of the time — or worse, decide the screen is broken.
  //
  // Nothing here is invented: every answer comes from something the page
  // already told the browser, and the same things a screen reader would say.
  // When none of them speaks, this says nothing rather than making something
  // up — a confident wrong reason is worse than no reason, because it sends
  // the run somewhere specific.
  const textOf=(ids)=>String(ids||'').split(/\s+/).filter(Boolean)
    .map(id=>{ const n=document.getElementById(id); return n && visible(n) ? n.innerText.trim() : ''; })
    .filter(Boolean).join(' ').slice(0,120);
  const whyBlocked=(e)=>{
    // The browser's own explanation, which is also the one the user sees.
    try { if (e.willValidate && !e.validity?.valid && e.validationMessage)
      return e.validationMessage.slice(0,120); } catch {}
    const described=textOf(e.getAttribute('aria-errormessage')) ||
                    textOf(e.getAttribute('aria-describedby'));
    if (described) return described;
    // A whole section switched off explains every control inside it, and is
    // the difference between "fill this in" and "this step is not yours yet".
    const fieldset=closestDeep(e,'fieldset[disabled],[aria-disabled="true"]');
    if (fieldset && fieldset!==e) {
      const legend=fieldset.querySelector('legend')?.innerText?.trim() || name(fieldset);
      return legend ? `inside "${legend.slice(0,60)}", which is disabled` : 'inside a disabled section';
    }
    // El `title` NO sirve aqui, y medirlo lo demostro: en el selector real
    // los botones de paginacion devolvieron "Primera fila" y "Bloque
    // anterior" como razon de estar deshabilitados. Eso no es una razon, es
    // el nombre del boton — y un motivo con aplomo y equivocado es peor que
    // ninguno, porque manda la corrida a un sitio concreto que no existe.
    // The distinction CDP already makes and no client passes on: a control
    // marked aria-disabled is disabled by the APPLICATION, not by the form.
    // That is a rule somewhere, not a missing field, and it is usually a
    // permission or a state the document is in.
    if (e.getAttribute('aria-disabled')==='true' && !e.matches(':disabled'))
      return 'the application disabled it, not the form';
    return null;
  };
  for (const e of crossRoots(selector)) {
    if (!safe(e) || !visible(e)) continue;
    if (e.matches(':disabled') || e.closest('[aria-disabled="true"]') ||
        e.getAttribute('aria-disabled')==='true') {
      const r=e.getBoundingClientRect();
      if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight) {
        const label=name(e)||role(e);
        if (label && blocked.length<MAX_BLOCKED) {
          const why=whyBlocked(e);
          blocked.push(why ? {role:role(e),label:label.slice(0,80),why}
                           : {role:role(e),label:label.slice(0,80)});
        }
      }
      continue;
    }
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // A control inside a row needs the row to be nameable. A grid of 122 rows
    // offers 122 controls all called "checkbox": indistinguishable, so the
    // model cannot pick "the one on the first row" and stalls. Screen readers
    // hit the same wall, so borrowing the row's own text is the same fix.
    let label=name(e)||rname;
    const generic=!name(e) || ['checkbox','radio','button','gridcell'].includes(label.toLowerCase());
    if (generic) {
      const row=closestDeep(e,'tr,[role="row"]');
      if (row) {
        // A control in the header row acts on EVERY row. Left looking like the
        // others, "select the first row" ticks select-all instead: observed on
        // a 122-row picker, where it selected all 122.
        const header=!!closestDeep(e,'thead,[role="rowgroup"][class*="head" i]') ||
          !!row.querySelector('th,[role="columnheader"]');
        if (header) {
          label=label+' (todas las filas)';
        } else {
          const own=(row.innerText||'').replace(/\s+/g,' ').trim().slice(0,60);
          if (own) label=label+' — '+own;
        }
      }
    }
    const base={node:identity(e),role:rname,label,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    // ── Lo que la pagina DECLARA sobre este campo ─────────────────────────
    //
    // Un agente que no lee esto tiene que descubrir las obligaciones fallando:
    // rellena lo que ve, guarda, le rechazan, vuelve. Y a veces ni eso — el
    // rechazo llega como un aviso que no sabe relacionar con un campo, y se
    // queda reintentando OTRA cosa. Visto en una compra del ERP: sesenta pasos
    // repitiendo el selector de proveedor porque lo que faltaba era un numero de
    // control que nadie le habia dicho que era obligatorio.
    //
    // La alternativa es escribir un caso a mano por pantalla, y eso no escala a
    // "operar cualquier web": en cuanto sales de las pantallas preparadas, el
    // agente vuelve a estar ciego.
    //
    // Nada de esto se inventa. Son las mismas senales que usa un lector de
    // pantalla y que el navegador ya valida por su cuenta: el atributo del HTML,
    // el ARIA que el autor puso, y el mensaje que el propio navegador daria.
    const declarado = {};
    if (e.required || e.getAttribute('aria-required') === 'true') declarado.obligatorio = true;
    if (e.getAttribute('aria-invalid') === 'true') declarado.invalido = true;
    // `validity` es la validacion del navegador, que ya sabe si un email no es un
    // email o si falta un campo requerido — sin que nadie escriba una regla.
    try {
      if (e.validity && !e.validity.valid) {
        declarado.invalido = true;
        if (e.validationMessage) declarado.porque = e.validationMessage.slice(0, 120);
      }
    } catch {}
    const dicho = textOf(e.getAttribute('aria-errormessage')) || textOf(e.getAttribute('aria-describedby'));
    // El mensaje del autor manda sobre el del navegador: es el que explica la
    // regla de negocio, no solo que el formato no cuadra.
    if (dicho) declarado.porque = dicho.slice(0, 160);
    for (const [attr, clave] of [['pattern','patron'], ['maxlength','maximo'],
                                 ['minlength','minimo'], ['min','desde'], ['max','hasta'],
                                 ['inputmode','teclado'], ['placeholder','ejemplo']]) {
      const v = e.getAttribute(attr);
      if (v) declarado[clave] = String(v).slice(0, 80);
    }
    // `title` en un campo con patron es, por convencion, la explicacion del
    // formato que se espera. Sin patron suele ser una ayuda cualquiera.
    if (e.getAttribute('pattern') && e.title) declarado.formato = e.title.slice(0, 120);
    if (e.type && !['text','button','submit'].includes(e.type)) declarado.tipo = e.type;
    if (Object.keys(declarado).length) base.declarado = declarado;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // Second pass: everything that behaves like a control without being one.
  // Geometry is checked BEFORE the computed style, which is the expensive call:
  // on a large application this walks thousands of nodes, and most are rejected
  // by a rectangle that costs nothing.
  for (const e of crossRoots('*')) {
    const r=e.getBoundingClientRect();
    if (r.width<16 || r.height<16) continue;
    const x=r.x+r.width/2, y=r.y+r.height/2;
    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (!visible(e) || !looksClickable(e)) continue;
    const own=(e.innerText||e.getAttribute('aria-label')||e.getAttribute('title')||'')
      .replace(/\s+/g,' ').trim();
    // Without text there is nothing to tell it apart from its neighbours, and
    // an unnameable target is one the model cannot choose on purpose.
    if (!own) continue;
    actions.push({node:identity(e),role:e.getAttribute('role')||'clickable',
      label:own.slice(0,80),kind:'click',value:'',
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
  }
  // Third pass: cells that open an editor on DOUBLE click.
  //
  // A grid whose cells edit in place is invisible to a single click, so the
  // whole capture flow of a document — the lines of an invoice, the quantities,
  // the prices — cannot be driven at all. The cell is not an input until the
  // second click creates one, so nothing in the first two passes matches it.
  //
  // A double click costs nothing where it does nothing, so the cell is offered
  // wherever one could plausibly open something: a real gridcell, or a cell
  // that says it is editable.
  for (const e of crossRoots('td,th,[role="gridcell"],[role="columnheader"],[class*="editable" i]')) {
    const r=e.getBoundingClientRect();
    if (r.width<16 || r.height<16) continue;
    const x=r.x+r.width/2, y=r.y+r.height/2;
    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (!visible(e)) continue;
    // A cell whose content is already a control is driven through that control.
    if (e.querySelector(selector)) continue;
    const own=(e.innerText||'').replace(/\s+/g,' ').trim();
    if (!own) continue;
    // Name it by its row, the same way the controls above are: a hundred cells
    // called "12,00" are a hundred indistinguishable targets.
    const row=closestDeep(e,'tr,[role="row"]');
    const context=row ? (row.innerText||'').replace(/\s+/g,' ').trim().slice(0,60) : '';
    actions.push({node:identity(e),role:'cell',kind:'dblclick',value:own.slice(0,120),
      label:'Edit cell '+own.slice(0,40)+(context ? ' — '+context : ''),
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
  }
  // Upload pass: `safe` keeps file inputs out of the control passes so the model
  // never types into one, but the agent still needs a way to attach a file. A
  // file input is almost always hidden behind a button and opens a native OS
  // dialog no driver can reach, so a plain click is a dead end. Offer each file
  // input as its own `upload` action instead; the executor resolves the input
  // (even hidden, even behind its button) and hands it the file provided to the
  // run via DOM.setFileInputFiles. No dialog is ever opened. The action is only
  // useful when a file was passed to the run, but naming it always is harmless
  // and lets the model see the control exists.
  for (const e of crossRoots('input[type=file]')) {
    if (e.disabled) continue;
    let label=name(e);
    if (!label) {
      const trigger=e.closest('label') ||
        (e.id && document.querySelector('label[for="'+CSS.escape(e.id)+'"]')) ||
        e.closest('button,[role="button"]');
      label=trigger ? (name(trigger) || (trigger.innerText||'').replace(/\s+/g,' ').trim()) : '';
    }
    if (!label) label=e.getAttribute('aria-label') || e.getAttribute('data-testid') || 'file upload';
    const r=e.getBoundingClientRect();
    actions.push({node:identity(e),role:'button',kind:'upload',value:'',
      label:'Upload file to '+label.slice(0,60),
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
  }
  const words=[]; const range=document.createRange(); let node,length=0;
  const walkers=textRoots().map(r=>document.createTreeWalker(r,NodeFilter.SHOW_TEXT));
  const nextText=()=>{ while (walkers.length) { const n=walkers[0].nextNode(); if (n) return n; walkers.shift(); } return null; };
  while ((node=nextText()) && length<MAX_TEXT) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,MAX_TEXT), height=document.documentElement.scrollHeight;
  // Keyboard shortcuts the screen actually offers, read off the screen itself.
  //
  // A key is the cheapest way to drive an application: nothing to find, nothing
  // to hit-test, no dependence on how the button is laid out at this width. But
  // WHICH keys exist is a property of the screen, not of the app — a listing has
  // no Save — and a hardcoded table in a prompt goes stale the day one moves.
  //
  // So they are discovered, not assumed. A well-built app writes the key on the
  // control (in a `kbd` chip, or in the title so the tooltip carries it), for
  // the same reason it matters here: a shortcut nobody can see is a shortcut
  // nobody uses. That convention is the source — if the key is not written
  // anywhere, it was not offered to the user either, and an agent leaning on it
  // would be relying on something no human could have discovered.
  //
  // Reported with the enabled state, because a key wired to a disabled control
  // is inert: pressing it is not a failure of the app, and a run that treats
  // silence as a bug reports its own mistake.
  const keys=[], keyseen=new Set();
  const KEY=/^(F(?:[1-9]|1[0-2])|Esc|Escape|Supr|Del|Delete|Ins|Insert|Intro|Enter|Tab)$/i;
  const addKey=(raw,host)=>{
    const key=String(raw||'').trim();
    if (!KEY.test(key) || keys.length>=MAX_KEYS) return;
    const control=host&&closestDeep(host,'button,[role="button"],a[href],[role="menuitem"],[title]')||host;
    if (!control || !visible(control)) return;
    // The label is the control minus the key chip itself, so it reads "Guardar"
    // and not "Guardar F2" — the key is already the other half of the pair.
    const label=(name(control)||control.innerText||'').replace(new RegExp('\\(?\\b'+key+'\\b\\)?','ig'),'')
      .replace(/\s+/g,' ').trim().slice(0,60);
    const id=key.toUpperCase()+'|'+label.toLowerCase();
    if (!label || keyseen.has(id)) return;
    keyseen.add(id);
    const off=control.matches(':disabled')||control.getAttribute('aria-disabled')==='true'||
      !!control.closest('[aria-disabled="true"]');
    keys.push(off?{key,label,disabled:true}:{key,label});
  };
  for (const e of crossRoots('kbd')) addKey(e.textContent,e);
  for (const e of crossRoots('[title],[aria-keyshortcuts]')) {
    const shortcut=e.getAttribute('aria-keyshortcuts');
    if (shortcut) { addKey(shortcut,e); continue; }
    const m=/\(([^()]{1,10})\)\s*$/.exec(e.getAttribute('title')||'');
    if (m) addKey(m[1],e);
  }
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  // ── El tope no se puede comer el paso siguiente ───────────────────────
  //
  // El tope existe para una tabla densa, donde recoger mil celdas no ayuda a
  // nadie. Las opciones de un desplegable abierto son otra cosa: son el UNICO
  // paso que continua lo que se acaba de escribir. Y son las ultimas en
  // recogerse, porque un autocompletado se pinta en un portal al final del
  // documento — de modo que en una pantalla con muchos controles son
  // exactamente lo que el tope descarta.
  //
  // Medido: el campo queda escrito, ninguna sugerencia ofrecida, y el recorrido
  // se para a un clic del final diciendo que no puede seguir. Es el fallo que
  // se vio en las aptitudes y el sector de LinkedIn, que no tenia nada que ver
  // con no saber elegir una sugerencia: no se le ofrecia ninguna.
  const openCombobox=[...crossRoots('[aria-expanded="true"]')].some(
    e=>e.getAttribute('role')==='combobox' || e.hasAttribute('aria-autocomplete'));
  const urgent=[], rest=[];
  for (const a of actions) {
    let emergent=false;
    if (a.role==='option') {
      // Con un combobox declarado abierto basta. Si no lo declara —que pasa, y
      // mas de lo que deberia— sirve la senal que el autor ya le dio al
      // usuario: un desplegable emergente se pinta flotando, no en el flujo.
      if (openCombobox) emergent=true;
      else {
        const e=cache.nodes.get(a.node);
        const box=e && (closestDeep(e,'[role="listbox"],[role="menu"]') || e.parentElement);
        emergent=!!box && ['absolute','fixed'].includes(getComputedStyle(box).position);
      }
    }
    (emergent ? urgent : rest).push(a);
  }
  const ordered=[...urgent,...rest];
  const omitted_actions=Math.max(0,ordered.length-MAX_ACTIONS);
  ordered.splice(MAX_ACTIONS);
  actions.length=0;
  actions.push(...ordered);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  // One scroll action per container that actually scrolls, aimed at that
  // container. The page-level pair above stays: on an ordinary document it is
  // still the right move, and these only appear where there is something else
  // to move. Each is labelled with what it holds, so "scroll the table" and
  // "scroll the side panel" are not the same anonymous choice.
  let scroller=0;
  for (const {e,r,downwards,sideways} of scrollables()) {
    const id=identity(e);
    const what=(e.getAttribute('aria-label')||e.getAttribute('role')||e.tagName.toLowerCase())
      .slice(0,40);
    const at=++scroller;
    if (downwards && e.scrollTop+e.clientHeight<e.scrollHeight-2)
      actions.push({id:'scroll_in_'+at+'_down',kind:'scroll',node:id,
        label:'Scroll down inside '+what,delta:Math.max(200,Math.round(r.height*0.8))});
    if (downwards && e.scrollTop>0)
      actions.push({id:'scroll_in_'+at+'_up',kind:'scroll',node:id,
        label:'Scroll up inside '+what,delta:-Math.max(200,Math.round(r.height*0.8))});
    if (sideways && e.scrollLeft+e.clientWidth<e.scrollWidth-2)
      actions.push({id:'scroll_in_'+at+'_right',kind:'scroll',node:id,axis:'x',
        label:'Scroll right inside '+what,delta:Math.max(200,Math.round(r.width*0.8))});
    if (sideways && e.scrollLeft>0)
      actions.push({id:'scroll_in_'+at+'_left',kind:'scroll',node:id,axis:'x',
        label:'Scroll left inside '+what,delta:-Math.max(200,Math.round(r.width*0.8))});
  }
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,blocked,keys,marker,page_key,guards,omitted_actions,
    // Que se observo, no solo lo observado. Una observacion parcial que no se
    // declara es una observacion que miente por omision.
    scope:SCOPE,scope_missing,ignored:IGNORE};
})()
