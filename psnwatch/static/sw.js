// Minimal service worker: makes the dashboard installable and shows the last page when offline.
const CACHE = "psn-v1";
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/psn", "/psn/icon.svg"]))); self.skipWaiting(); });
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !url.pathname.startsWith("/psn")) return;
  // always try the network first (stock data must be fresh); fall back to cache offline
  e.respondWith(fetch(e.request).then((r) => {
    if (url.pathname === "/psn" && r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
