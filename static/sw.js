// Minimal service worker: lets the page be installed and shows a friendly message instead of the
// browser's error page when the watchcat server can't be reached. It caches nothing, so the page is
// always the live one.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
const OFFLINE = '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">' +
  '<title>watchcat</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0e13;color:#e8ebf2;' +
  'font:16px system-ui,sans-serif;text-align:center"><div><div style="font-size:42px">🐱</div><p><b>Can\'t reach watchcat</b></p>' +
  '<p style="color:#7d869a">The server or your network is down.</p><button onclick="location.reload()" ' +
  'style="padding:10px 18px;border-radius:10px;border:0;background:#7c9cff;color:#fff;font:inherit">Retry</button></div>';
self.addEventListener("fetch", e => {
  if (e.request.mode === "navigate")
    e.respondWith(fetch(e.request).catch(() => new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
