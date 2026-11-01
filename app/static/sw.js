// MyTrack service worker.
// App shell is cached so the app opens instantly and works offline (diary needs the server).
// Bump VERSION after changing files to roll out an update; the app reloads itself.
const VERSION = 'mytrack-v16';
const SHELL = ['/', '/offline.html', '/html5-qrcode.min.js', '/manifest.json', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png', '/favicon-32.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, u = new URL(req.url);
  if (req.method !== 'GET' || u.origin !== location.origin || u.pathname.startsWith('/api/')) return;
  if (req.mode === 'navigate') {
    // pages: network first so a hot-swapped index.html shows straight away
    e.respondWith(fetch(req).then(r => { if (r.ok && !r.redirected && r.type === 'basic') { const c = r.clone(); caches.open(VERSION).then(x => x.put('/', c)); } return r; })
      .catch(() => caches.match('/').then(r => r || caches.match('/offline.html'))));
    return;
  }
  // static files: cache first, refresh in background
  e.respondWith(caches.match(req).then(hit => {
    const net = fetch(req, { credentials: 'same-origin' }).then(r => { if (r.ok && !r.redirected && r.type === 'basic') { const c = r.clone(); caches.open(VERSION).then(x => x.put(req, c)); } return r; }).catch(() => hit);
    return hit || net;
  }));
});

// ---- push notifications
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: 'MyTrack', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'MyTrack', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-maskable-192.png', tag: d.tag || 'mytrack', renotify: true,
    data: { url: d.url || '/' }
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if ('focus' in w) { w.navigate(url).catch(() => {}); return w.focus(); } }
    return clients.openWindow(url);
  }));
});
