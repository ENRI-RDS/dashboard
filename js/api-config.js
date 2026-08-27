/**
 * ENRI Dashboard — API config helper
 * ----------------------------------
 * Drop-in helper for the static HTML pages on GitHub Pages.
 *
 * If you set `window.ENRI_API_BASE` (or save it in localStorage as
 * `enri_api_base`), the helper rewrites every fetch() targeting a CSV /
 * GeoJSON / JSON file to go through your backend API instead of the
 * static file on GitHub Pages.
 *
 * Usage in each HTML page (single line in <head>):
 *   <script src="js/api-config.js"></script>
 *
 * Then to point all pages at the backend, run once in the browser console:
 *   localStorage.setItem('enri_api_base', 'https://enri-dashboard-api.onrender.com')
 *
 * To go back to static files, clear it:
 *   localStorage.removeItem('enri_api_base')
 */
(function () {
  const DEFAULT_API_BASE = 'https://enri-dashboard-api.onrender.com';

  const API_BASE = (window.ENRI_API_BASE
    || localStorage.getItem('enri_api_base')
    || DEFAULT_API_BASE
    || '').replace(/\/$/, '');

  window.ENRI = {
    apiBase: API_BASE,
    isEnabled: !!API_BASE,
    /** Returns the URL where the given data file should be fetched from. */
    dataUrl(path) {
      const clean = String(path).replace(/^\.?\//, '');
      return API_BASE ? `${API_BASE}/api/data/${clean}` : clean;
    },
  };

  if (!API_BASE) return; // no backend configured → behave like before

  // Transparent fetch() rewrite for CSV / GeoJSON / JSON requests pointing to
  // local files. Absolute URLs and explicit /api/ calls are left untouched.
  const origFetch = window.fetch.bind(window);
  const DATA_RE = /\.(csv|geojson|json)(?:\?.*)?$/i;

  window.fetch = function (input, init) {
    try {
      let url = typeof input === 'string' ? input : (input && input.url) || '';
      const isAbsolute = /^https?:\/\//i.test(url);
      const isApi = url.includes('/api/');
      if (!isAbsolute && !isApi && DATA_RE.test(url)) {
        const rewritten = `${API_BASE}/api/data/${url.replace(/^\.?\//, '')}`;
        if (typeof input === 'string') input = rewritten;
        else input = new Request(rewritten, input);
      }
    } catch (_) { /* fall through */ }

    // Inietta x-session-token su ogni chiamata diretta al nostro backend
    // (data-file riscritti sopra + /api/... espliciti), a meno che il
    // chiamante non l'abbia già impostato esplicitamente. Evita la classe di
    // bug "fetch senza header token → 401 silenzioso" ricorsa più volte nello
    // storico (v. AGENT_BRIEF rev.129/130/135/136/149).
    try {
      const finalUrl = typeof input === 'string' ? input : input.url;
      if (API_BASE && finalUrl && finalUrl.startsWith(API_BASE)) {
        const token = localStorage.getItem('_enri_session') || '';
        const existingHeaders = (init && init.headers) || (input instanceof Request ? input.headers : undefined);
        const headers = new Headers(existingHeaders);
        if (token && !headers.has('x-session-token')) headers.set('x-session-token', token);
        init = { ...(init || {}), headers };
      }
    } catch (_) { /* fall through */ }

    return origFetch(input, init);
  };

  console.info('[ENRI] API base active →', API_BASE);
})();

// ── Concomitanza tratte (es. ENRI-QTS, tubo aggiuntivo) ─────────────────────
// Caricata una volta per pagina, usata ovunque compaia un codice pratica.
window.ENRI = window.ENRI || {};
window.ENRI.concomitanzaIds = new Set();
window.ENRI.concomitanzaNota = {};
window.ENRI.concomitanzaReady = window.ENRI.apiBase
  ? fetch(`${window.ENRI.apiBase}/api/concomitanze`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) return;
        window.ENRI.concomitanzaIds = new Set(data.tratta_ids || []);
        window.ENRI.concomitanzaNota = data.nota_by_tratta || {};
      })
      .catch(() => {})
  : Promise.resolve();

/**
 * Badge HTML da affiancare a un codice pratica quando una o più tratte
 * associate sono in concomitanza (es. tubo aggiuntivo). `trattaIds` può
 * essere un singolo TRATTA_ID o un array (una pratica può coprire più
 * tratte). Ritorna '' se nessuna tratta è in concomitanza.
 */
window.ENRI.concomitanzaBadge = function (trattaIds) {
  const ids = (Array.isArray(trattaIds) ? trattaIds : [trattaIds])
    .map(t => String(t || '').trim().toUpperCase()).filter(Boolean);
  const hit = ids.filter(t => window.ENRI.concomitanzaIds.has(t));
  if (!hit.length) return '';
  const nota = window.ENRI.concomitanzaNota[hit[0]] || 'Tubo aggiuntivo';
  const label = hit.length > 1 ? `${nota} (${hit.length} tratte)` : nota;
  return `<span class="enri-concomitanza-badge" title="Concomitanza QTS: ${label}" `
    + `style="display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;`
    + `margin-left:3px;border-radius:50%;background:#1A7D9922;color:#1A7D99;border:1px solid #1A7D9966;`
    + `font-size:9px;font-weight:800;cursor:help;vertical-align:middle">T</span>`;
};
