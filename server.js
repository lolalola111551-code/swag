// LINGUA — прокси-сервер для Ollama API
// Запуск: node server.js
// Требует: Node.js 18+

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = process.env.PORT || 3000;
const HOST = '0.0.0.0';

// ─── API КЛЮЧ — только на сервере, не в браузере ──────────
// На хостинге: задай переменную окружения OLLAMA_API_KEY
// Локально: OLLAMA_API_KEY=твой_ключ node server.js
const OLLAMA_API_KEY = process.env.OLLAMA_API_KEY || '';

if (!OLLAMA_API_KEY) {
  console.warn('⚠️  ВНИМАНИЕ: переменная OLLAMA_API_KEY не задана!');
}

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

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  // ─── PROXY /api/chat → Ollama ────────────────────────────
  // Ключ НЕ принимается от клиента — всегда используем серверный
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

      const postData = Buffer.from(JSON.stringify(payload));
      const options = {
        hostname: OLLAMA_API_HOST,
        path: OLLAMA_API_PATH,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': postData.length,
          // Ключ подставляется с сервера — клиент его никогда не видит
          'Authorization': `Bearer ${OLLAMA_API_KEY}`,
        },
      };

      const proxyReq = https.request(options, (proxyRes) => {
        res.writeHead(proxyRes.statusCode, { 'Content-Type': 'application/json' });
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

server.listen(PORT, HOST, () => {
  console.log(`Сервер запущен → http://${HOST}:${PORT}`);
  console.log(`Прокси: /api/chat → https://${OLLAMA_API_HOST}${OLLAMA_API_PATH}`);
  console.log(`API ключ: ${OLLAMA_API_KEY ? '✓ задан' : '✗ НЕ задан (задай OLLAMA_API_KEY)'}`);
});
