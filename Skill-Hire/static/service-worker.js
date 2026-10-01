const CACHE='hirenow-brand-v3';
const BRAND_ASSETS=['/static/brand/hirenow-logo.webp','/static/brand/final-brand.css','/static/brand/final-brand.js'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(BRAND_ASSETS)));self.skipWaiting();});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method!=='GET'||url.origin!==self.location.origin)return;
  if(event.request.mode==='navigate'&&(url.pathname==='/'||url.pathname==='/worker')){event.respondWith(fetch(event.request).then(async response=>{if(!response.ok)return response;const type=response.headers.get('content-type')||'';if(!type.includes('text/html'))return response;let html=await response.text();const brand=`<link rel="stylesheet" href="/static/brand/final-brand.css?v=3"><script src="/static/brand/final-brand.js?v=3" defer></script>`;html=html.replace('</head>',brand+'</head>');const headers=new Headers(response.headers);headers.delete('content-length');return new Response(html,{status:response.status,statusText:response.statusText,headers});}).catch(()=>fetch(event.request)));return;}
  if(!url.pathname.startsWith('/static/brand/'))return;event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request).then(response=>{if(response.ok)caches.open(CACHE).then(cache=>cache.put(event.request,response.clone()));return response;})));
});
