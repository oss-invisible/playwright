// A service worker that answers one navigation itself: the page it builds carries what the worker's own realm reads and the headers the
// worker saw on the request, so a controlled navigation is read through the worker, not around it.
importScripts('/ab/realm.js' + location.search);
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname !== '/ab/controlled.html') return;
  e.respondWith((async () => {
    const data = await abRealm();
    data.request = {acceptLanguage: e.request.headers.get('accept-language'), mode: e.request.mode, destination: e.request.destination};
    try { data.own_fetch = (await fetch('/ab/probe' + u.search)).status; } catch (x) { data.own_fetch = 'ERR ' + ((x && x.name) || ''); }
    const json = JSON.stringify(data).replace(/</g, '\\u003c');
    return new Response('<!doctype html><meta charset=utf-8><title>controlled</title><script type="application/json" id="sw">' + json + '</script>',
                        {headers: {'Content-Type': 'text/html; charset=utf-8'}});
  })());
});
