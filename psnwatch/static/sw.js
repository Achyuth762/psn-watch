// Service worker: makes the dashboard installable, shows the last page offline,
// and opens the store when a restock notification is tapped.
const CACHE = "psn-v3";
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/psn", "/psn/icon.svg"]))); self.skipWaiting(); });
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !url.pathname.startsWith("/psn")) return;
  // network first: stock data must be fresh; fall back to the cached page offline
  e.respondWith(fetch(e.request).then((r) => {
    if (url.pathname === "/psn" && r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request)));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/psn";
  e.waitUntil(self.clients.openWindow(url));
});
