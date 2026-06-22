const CACHE = 'zilloagent-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => { try { c.put(e.request, copy); } catch(_){} });
      return r;
    }).catch(() => caches.match(e.request))
  );
});
