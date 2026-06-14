const http = require('http');
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const PORT = process.env.PORT || 3000;
const MIME = {'.html':'text/html;charset=utf-8','.js':'application/javascript;charset=utf-8','.css':'text/css','.json':'application/json','.csv':'text/csv;charset=utf-8','.geojson':'application/geo+json','.png':'image/png','.svg':'image/svg+xml'};
http.createServer((req, res) => {
  let url = decodeURIComponent(req.url.split('?')[0]);
  if (url === '/') url = '/hub.html';
  const file = path.join(ROOT, url);
  if (!file.startsWith(ROOT)) { res.writeHead(403); return res.end('forbidden'); }
  fs.stat(file, (e, st) => {
    if (e || !st.isFile()) { res.writeHead(404, {'Content-Type':'text/plain'}); return res.end('Not found: '+url); }
    res.writeHead(200, {'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream', 'Cache-Control':'no-cache'});
    fs.createReadStream(file).pipe(res);
  });
}).listen(PORT, '0.0.0.0', () => console.log('static server on', PORT));
