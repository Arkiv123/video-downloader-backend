/* Service worker: makes the site installable (PWA) and instant on repeat
   visits, WITHOUT ever caching a downloaded video or an API response.

   Strategy is deliberately narrow:
   - Only same-origin GET navigations/assets use a network-first-with-cache
     fallback, so the shell loads offline and feels instant.
   - The backend API (POST /formats, /download) and any cross-origin request
     (CDN media streams) are NEVER intercepted — those must always hit the
     network fresh, and media must never sit in the cache and blow the quota. */

const CACHE = "gr-shell-v1";

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  // Only handle same-origin GETs. Leave POST (API) and cross-origin (CDN) alone.
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  e.respondWith(
    fetch(req)
      .then((res) => {
        // Cache a copy of successful shell responses for offline use.
        if (res && res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
