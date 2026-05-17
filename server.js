// LINGUA — локальный прокси-сервер для Ollama API
// Запуск: node server.js
// Требует: Node.js 18+

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = 3000;
const OLLAMA_API_HOST = 'ollama.com';
const OLLAMA_API_PATH = '/api/chat';

// ─── MIME TYPES ───────────────────────────────────────────
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'application/javascript',
  '.css':  'text/css',
  '.json': 'application/json',
  '.png':  'image/png',
  '.ico':  'image/x-icon',
};

// ─── SERVER ───────────────────────────────────────────────
const server = http.createServer((req, res) => {
  const parsed = url.parse(req.url);

  // CORS headers — нужны для запросов из браузера
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  // ─── PROXY /api/chat → Ollama cloud ───────────────────
  if (req.method === 'POST' && parsed.pathname === '/api/chat') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      let payload;
      try { payload = JSON.parse(body); } catch {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Invalid JSON' }));
        return;
      }

      // Достаём Authorization из запроса браузера
      const authHeader = req.headers['authorization'] || '';

      const postData = Buffer.from(JSON.stringify(payload));
      const options = {
        hostname: OLLAMA_API_HOST,
        path: OLLAMA_API_PATH,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': postData.length,
          'Authorization': authHeader,
        },
      };

      const proxyReq = https.request(options, (proxyRes) => {
        res.writeHead(proxyRes.statusCode, {
          'Content-Type': 'application/json',
        });
        proxyRes.pipe(res);
      });

      proxyReq.on('error', (e) => {
        console.error('Ошибка запроса к Ollama:', e.message);
        res.writeHead(502, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Proxy error: ' + e.message }));
      });

      proxyReq.write(postData);
      proxyReq.end();
    });
    return;
  }

  // ─── STATIC FILES ─────────────────────────────────────
  let filePath = parsed.pathname === '/' ? '/index.html' : parsed.pathname;
  filePath = path.join(__dirname, filePath);

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(data);
  });
});

server.listen(PORT, () => {
  console.log('');
  console.log('  ██╗     ██╗███╗   ██╗ ██████╗ ██╗   ██╗ █████╗ ');
  console.log('  ██║     ██║████╗  ██║██╔════╝ ██║   ██║██╔══██╗');
  console.log('  ██║     ██║██╔██╗ ██║██║  ███╗██║   ██║███████║');
  console.log('  ██║     ██║██║╚██╗██║██║   ██║██║   ██║██╔══██║');
  console.log('  ███████╗██║██║ ╚████║╚██████╔╝╚██████╔╝██║  ██║');
  console.log('  ╚══════╝╚═╝╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝');
  console.log('');
  console.log(`  Сервер запущен → http://localhost:${PORT}`);
  console.log(`  Прокси: /api/chat → https://${OLLAMA_API_HOST}${OLLAMA_API_PATH}`);
  console.log('');
  console.log('  Открой в браузере: http://localhost:3000');
  console.log('  Останови: Ctrl+C');
  console.log('');
});
