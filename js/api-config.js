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
    return origFetch(input, init);
  };

  console.info('[ENRI] API base active →', API_BASE);
})();
