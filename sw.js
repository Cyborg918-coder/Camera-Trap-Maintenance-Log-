// Service Worker - 离线缓存 (红外相机维护日志)
// 作用: 出发前打开一次 → 页面缓存到手机 → 野外无信号也能打开
const CACHE = 'ircam-v2';   // 2026-09-29 升级：清理测试数据 + 缓存迁移，强制队员端刷新
const ASSETS = ['./', './index.html', './manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  // API请求: 网络优先(离线则失败, 数据在本地)
  if (req.url.includes('/api/')) {
    e.respondWith(fetch(req).catch(() => new Response('{"offline":true}', {headers:{'Content-Type':'application/json'}})));
    return;
  }
  // 页面/静态资源: 缓存优先(离线可用), 后台更新
  e.respondWith(
    caches.match(req).then(cached => {
      const netFetch = fetch(req).then(resp => {
        if (resp && resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(req, clone));
        }
        return resp;
      }).catch(() => cached);
      return cached || netFetch;
    })
  );
});
