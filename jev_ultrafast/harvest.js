(() => {
  // Lo que la pantalla REPITE, como datos.
  //
  // Una lista de mercados, una tabla de precios, un tablero de noticias: en
  // cuanto una pagina enseña muchas cosas del mismo tipo, lo que interesa ya no
  // es que se puede pulsar sino QUE DICE. Y eso no se saca con una accion por
  // elemento: son cuarenta tarjetas y el que pregunta quiere las cuarenta, con
  // su titulo y su numero, en una estructura que pueda recorrer un programa.
  //
  // La repeticion se descubre, no se configura. Un selector escrito a mano
  // ata el resultado al HTML de hoy de un sitio ajeno, y ese HTML cambia sin
  // avisar; lo que no cambia es que una lista sigue siendo varias cosas
  // parecidas una detras de otra. Se agrupan por su FIRMA —etiqueta mas las
  // clases que comparten— y se queda el grupo que de verdad se repite.
  //
  // 🚨 Esto LEE. No pulsa nada, no escribe nada y no manda nada a ningun
  // sitio: devuelve lo que la pagina ya le esta enseñando a quien la mira.
  const cap = (name, fallback) => {
    const raw = window.__jevBudgets?.[name];
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
  };
  const MAX_GROUPS = cap('groups', 6);
  const MAX_ITEMS = cap('items', 60);
  const MIN_REPEATS = 3;

  const roots = [document];
  const seenRoot = new Set();
  const walk = (root) => {
    if (!root || seenRoot.has(root)) return;
    seenRoot.add(root);
    for (const e of root.querySelectorAll('*')) {
      if (e.shadowRoot) { roots.push(e.shadowRoot); walk(e.shadowRoot); }
    }
    for (const f of root.querySelectorAll('iframe,frame')) {
      let inner = null;
      try { inner = f.contentDocument; } catch { inner = null; }
      if (inner) { roots.push(inner); walk(inner); }
    }
  };
  walk(document);

  const visible = (e) => {
    try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); }
    catch { return true; }
  };
  // La firma de un elemento: que es, y con que clases. Se recortan a las tres
  // primeras porque los frameworks modernos añaden una clase unica por
  // instancia, y con esa dentro cada tarjeta seria su propio grupo de uno.
  const signature = (e) => {
    const classes = (typeof e.className === 'string' ? e.className : '')
      .split(/\s+/).filter(Boolean).slice(0, 3).sort().join('.');
    return e.tagName + (classes ? '.' + classes : '');
  };
  // El texto tal y como lo lee una persona: una linea por renglon, sin los
  // huecos que deja el maquetado.
  const lines = (e) => (e.innerText || '')
    .split('\n').map(s => s.trim()).filter(Boolean).slice(0, 12)
    .map(s => s.slice(0, 160));
  // Numeros con su unidad pegada, que es donde vive el significado: "62%" no
  // es 62, y "$1.2M" tampoco.
  const numbers = (text) => {
    const found = text.match(/[$€£]?\s?\d[\d.,]*\s?(?:%|[KMB]\b|millones?|mil)?/g) || [];
    return [...new Set(found.map(s => s.trim()).filter(s => /\d/.test(s)))].slice(0, 8);
  };

  const groups = [];
  for (const root of roots) {
    const byParent = new Map();
    const container = root.body || root;
    if (!container?.querySelectorAll) continue;
    for (const e of container.querySelectorAll('*')) {
      const parent = e.parentElement;
      if (!parent) continue;
      if (!byParent.has(parent)) byParent.set(parent, new Map());
      const buckets = byParent.get(parent);
      const key = signature(e);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(e);
    }
    for (const [parent, buckets] of byParent) {
      for (const [key, kids] of buckets) {
        if (kids.length < MIN_REPEATS) continue;
        const shown = kids.filter(visible);
        if (shown.length < MIN_REPEATS) continue;
        // Un grupo cuyos hijos no dicen nada es maquetado, no datos: filas de
        // separadores, celdas de espaciado, iconos sueltos.
        const items = [];
        for (const kid of shown.slice(0, MAX_ITEMS)) {
          const text = lines(kid);
          if (!text.length) continue;
          const link = kid.matches('a[href]') ? kid : kid.querySelector('a[href]');
          const entry = {text};
          if (link?.getAttribute('href')) entry.href = link.getAttribute('href').slice(0, 300);
          const nums = numbers(text.join(' '));
          if (nums.length) entry.numbers = nums;
          items.push(entry);
        }
        if (items.length < MIN_REPEATS) continue;
        // Cuanto texto distinto trae el grupo. Es lo que separa una lista de
        // datos de una barra de navegacion repetida: la navegacion dice lo
        // mismo en todas partes, los datos no.
        const distinct = new Set(items.map(i => i.text.join('|'))).size;
        if (distinct < MIN_REPEATS) continue;
        groups.push({
          // Como llamar a este grupo sin inventarse nada: lo que dice el
          // contenedor de si mismo, o su etiqueta.
          where: (parent.getAttribute?.('aria-label') || parent.id ||
                  parent.getAttribute?.('data-testid') || key).toString().slice(0, 60),
          shape: key.slice(0, 60),
          count: shown.length,
          shown: items.length,
          items,
        });
      }
    }
  }
  // El grupo mas interesante es el que mas cosas DISTINTAS dice, no el que
  // mas elementos tiene: mil celdas vacias no son mil datos.
  groups.sort((a, b) =>
    (new Set(b.items.map(i => i.text.join('|'))).size) -
    (new Set(a.items.map(i => i.text.join('|'))).size));
  return {
    url: location.href,
    title: document.title,
    groups: groups.slice(0, MAX_GROUPS),
    omitted_groups: Math.max(0, groups.length - MAX_GROUPS),
  };
})()
