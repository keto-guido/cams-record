/* Cameras PWA service worker: Web Push display + deep-link on click. Scope: /cams/ */
'use strict';

self.addEventListener('install', event => {
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

function payloadOf(event) {
  let data = {};
  try {
    if (event.data) data = event.data.json();
  } catch (e) {
    try { data = { body: event.data && event.data.text() }; } catch (e2) { data = {}; }
  }
  return data && typeof data === 'object' ? data : {};
}

self.addEventListener('push', event => {
  const data = payloadOf(event);
  const title = data.title || 'Cameras';
  const body = data.body || 'New detection';
  const url = data.url || '/cams/#detections';
  const tag = data.tag || 'cams-detection';
  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag,
    renotify: true,
    data: { url, event_id: data.event_id || null, camera_id: data.camera_id || null, label: data.label || null, start_ts: data.start_ts || null },
    icon: '/cams/static/icon-192.png',
    badge: '/cams/static/icon-192.png',
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = data.url || '/cams/#detections';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of all) {
      try {
        const u = new URL(client.url);
        if (u.pathname.startsWith('/cams') && 'focus' in client) {
          await client.focus();
          if ('navigate' in client) {
            try { await client.navigate(target); return; } catch (e) {}
          }
          client.postMessage({ type: 'cams-push-open', url: target, data });
          return;
        }
      } catch (e) {}
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
