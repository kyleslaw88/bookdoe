/* In-app EPUB reader: the same engine and position format as the web reader
 * (epub.js, EPUB CFI locations), so a book resumes where the other client left it.
 * Talks to the app through window.webkit.messageHandlers.alx and window.alx. */
(function () {
  const post = (type, data) => window.webkit.messageHandlers.alx.postMessage(Object.assign({ type }, data || {}));
  const THEMES = {
    light: { bg: '#ffffff', fg: '#1b1b1b', link: '#2d6fd6' },
    sepia: { bg: '#f4ecd8', fg: '#433422', link: '#8a5a1f' },
    dark: { bg: '#121212', fg: '#e6e6e6', link: '#7db3ff' },
  };
  const FONTS = {
    serif: 'Georgia, "Times New Roman", serif',
    sans: '-apple-system, "Helvetica Neue", Arial, sans-serif',
  };
  let prefs = { theme: 'light', size: 100, font: 'serif', spacing: 1.6, flow: 'paginated' };
  let book = null, rendition = null, lastCfi = null, startCfi = null, locationsReady = false, lastLoc = null;
  // Highlights the app wants drawn (id, cfiRange, color name) and bookmarks (id, cfi) to flag the current page.
  const COLORS = { yellow: '#f7d24a', green: '#7ed957', blue: '#6ab7ff', pink: '#ff8fb1' };
  let wantedHighlights = [], paintedHighlights = new Map(), bookmarkList = [];

  function applyPrefs(reflow) {
    const t = THEMES[prefs.theme] || THEMES.light;
    document.body.style.background = t.bg;
    if (!rendition) return;
    const size = prefs.size + '%';
    // Same overrides as the web reader: publisher stylesheets with absolute sizes
    // would otherwise defeat the text-size control.
    rendition.themes.default({
      html: { 'font-size': size + ' !important' },
      body: { 'font-size': '1em !important', color: t.fg + ' !important', background: t.bg + ' !important',
              'font-family': (FONTS[prefs.font] || FONTS.serif) + ' !important', 'line-height': prefs.spacing + ' !important',
              padding: '0 8px !important' },
      'p, div, li, blockquote, dd, dt, td, th, figcaption, pre': { 'font-size': 'inherit !important', 'line-height': prefs.spacing + ' !important' },
      'a, a:visited': { color: t.link + ' !important' },
    });
    rendition.themes.fontSize(size);
    if (reflow) { rendition.flow(prefs.flow); if (lastCfi) rendition.display(lastCfi); }
    paintedHighlights.forEach((h, id) => paint(id));   // the blend mode depends on the theme
  }

  function hlStyles(color) {
    const dark = prefs.theme === 'dark';
    return { fill: COLORS[color] || COLORS.yellow, 'fill-opacity': dark ? '0.5' : '0.4', 'mix-blend-mode': dark ? 'normal' : 'multiply' };
  }

  function paint(id) {
    const h = paintedHighlights.get(id);
    if (!h || !rendition) return;
    try { rendition.annotations.remove(h.cfiRange, 'highlight'); } catch (e) {}
    try { rendition.annotations.highlight(h.cfiRange, { id }, () => post('highlightTapped', { id }), 'alx-hl', hlStyles(h.color)); } catch (e) {}
  }

  /** Make the page show exactly this list of highlights (a full list each time). */
  function applyHighlights() {
    if (!rendition) return;
    paintedHighlights.forEach((h, id) => {
      if (!wantedHighlights.some(w => w.id === id)) {
        try { rendition.annotations.remove(h.cfiRange, 'highlight'); } catch (e) {}
        paintedHighlights.delete(id);
      }
    });
    wantedHighlights.forEach(w => {
      const old = paintedHighlights.get(w.id);
      paintedHighlights.set(w.id, { cfiRange: w.cfiRange, color: w.color });
      if (!old || old.cfiRange !== w.cfiRange || old.color !== w.color) paint(w.id);
    });
  }

  const dbg = m => post('log', { msg: String(m) });

  function reportBookmarkState() {
    let ids = [];
    if (lastLoc && lastLoc.start && lastLoc.end && window.ePub && window.ePub.CFI) {
      const cfi = new window.ePub.CFI();
      ids = bookmarkList.filter(b => {
        try { return cfi.compare(lastLoc.start.cfi, b.cfi) <= 0 && cfi.compare(b.cfi, lastLoc.end.cfi) <= 0; } catch (e) { dbg('compare failed: ' + e); return false; }
      }).map(b => b.id);
    } else {
      dbg('bookmark state skipped: loc=' + !!lastLoc + ' end=' + !!(lastLoc && lastLoc.end) + ' cfiClass=' + !!(window.ePub && window.ePub.CFI));
    }
    post('bookmarkState', { ids, listed: bookmarkList.length });
  }

  function percentFor(cfi) {
    if (!locationsReady || !book.locations || !book.locations.length()) return null;
    let p = null;
    try { p = book.locations.percentageFromCfi(cfi); } catch (e) { dbg('percent failed: ' + e); }
    return typeof p === 'number' && isFinite(p) ? Math.round(p * 1000) / 1000 : null;
  }

  function report(loc, moved) {
    if (!loc || !loc.start || !loc.start.cfi) return;
    lastCfi = loc.start.cfi;
    lastLoc = loc;
    let chapter = '';
    try {
      const spine = book.spine.get(lastCfi);
      const nav = spine && book.navigation && book.navigation.get(spine.href);
      if (nav && nav.label) chapter = nav.label.trim();
    } catch (e) {}
    post('relocated', { cfi: lastCfi, percent: percentFor(lastCfi), chapter, moved: moved !== false });
    try { reportBookmarkState(); } catch (e) { dbg('bookmark state failed: ' + e); }
  }

  function flatten(items, depth, out) {
    (items || []).forEach(i => { out.push({ label: (i.label || '').trim(), href: i.href, depth }); flatten(i.subitems, depth + 1, out); });
    return out;
  }

  async function open(opts) {
    prefs = Object.assign(prefs, (opts && opts.prefs) || {});
    startCfi = (opts && opts.start) || null;
    applyPrefs();
    try {
      const r = await fetch('book');
      if (!r.ok) throw new Error('Could not read the book file (' + r.status + ')');
      const buf = await r.arrayBuffer();
      book = window.ePub(buf);
      rendition = book.renderTo('view', { width: '100%', height: '100%', flow: prefs.flow, spread: 'none', allowScriptedContent: false });
      applyPrefs();
      rendition.on('relocated', loc => report(loc, true));
      applyHighlights();
      await rendition.display(startCfi || undefined);
      post('ready');
      book.loaded.navigation.then(nav => post('toc', { items: flatten(nav.toc, 0, []) })).catch(() => {});
      // Locations give a stable percentage; generating them is the slow part, so the
      // page is readable first and the percentage arrives when they are ready.
      book.ready.then(() => book.locations.generate(1200)).then(() => {
        locationsReady = true;
        const loc = rendition.currentLocation();
        if (loc && loc.start) report(loc, false);   // same page, now with a percentage
      }).catch(() => {});
    } catch (e) {
      post('error', { message: String((e && e.message) || e) });
    }
  }

  // Selecting text (markup mode). WKWebView does not fire selectionchange for the page's iframe, so the
  // selection is polled while markup mode is on: once it has held still for a tick it is reported with its
  // EPUB CFI range; an emptied selection is reported as cleared.
  let markupOn = false, pollTimer = null, polledText = '', reportedText = null;
  function pollSelection() {
    let text = '', range = null, contents = null;
    try {
      (rendition ? rendition.getContents() : []).forEach(c => {
        const s = c.window.getSelection();
        if (s && s.rangeCount && !s.isCollapsed && s.toString().trim()) { text = s.toString(); range = s.getRangeAt(0); contents = c; }
      });
    } catch (e) { dbg('selection poll failed: ' + e); }
    if (text !== polledText) {   // still changing: wait for it to hold still
      polledText = text;
      reportedText = null;
      if (!text) post('selectionCleared');
      return;
    }
    if (text && reportedText !== text) {
      reportedText = text;
      try {
        const cfiRange = new window.ePub.CFI(range, contents.cfiBase).toString();
        post('selected', { cfiRange, text: text.replace(/\s+/g, ' ').trim(), percent: percentFor(lastCfi) });
      } catch (e) { dbg('selection range failed: ' + e); }
    }
  }
  function setMarkup(on) {
    markupOn = !!on;
    clearInterval(pollTimer);
    polledText = ''; reportedText = null;
    if (markupOn) pollTimer = setInterval(pollSelection, 350);
  }

  window.alx = {
    open,
    next: () => rendition && rendition.next(),
    prev: () => rendition && rendition.prev(),
    goto: target => rendition && rendition.display(target),
    setPrefs: p => { Object.assign(prefs, p); applyPrefs(!!(p && p.flow)); },
    setMarkup,
    setHighlights: list => { wantedHighlights = list || []; applyHighlights(); },
    setBookmarks: list => { bookmarkList = list || []; reportBookmarkState(); },
    clearSelection: () => {
      try { rendition.getContents().forEach(c => c.window.getSelection().removeAllRanges()); } catch (e) {}
    },
  };
  post('loaded');
})();
