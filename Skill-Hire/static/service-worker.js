const CACHE='hirenow-brand-v6';
const BRAND_ASSETS=['/static/brand/hirenow-mark.svg?v=6','/static/brand/hirenow-lockup.svg?v=6','/static/brand/final-brand.css?v=6'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(BRAND_ASSETS)));self.skipWaiting();});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method!=='GET'||url.origin!==self.location.origin)return;if(!url.pathname.startsWith('/static/brand/'))return;event.respondWith(caches.match(event.request).then(c=>c||fetch(event.request).then(r=>{if(r.ok)caches.open(CACHE).then(cache=>cache.put(event.request,r.clone()));return r;})));});
