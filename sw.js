/* ============================================================
   Service Worker · 离线缓存
   只在 http(s) 下生效；file:// 打开时注册会失败，页面已做静默降级。
   改样式或内容后把 CACHE_VERSION 加一，旧缓存会在 activate 时清掉。
   ============================================================ */

const CACHE_VERSION = "ai-study-v1";

const PRECACHE = [
  "./",
  "./index.html",
  "./ai-params-guide.html",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png"
];

/* 安装：预缓存核心文件 */
self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(function (cache) { return cache.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

/* 激活：清掉旧版本缓存 */
self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(
          keys
            .filter(function (key) { return key !== CACHE_VERSION; })
            .map(function (key) { return caches.delete(key); })
        );
      })
      .then(function () { return self.clients.claim(); })
  );
});

/* 取用：缓存优先，联网成功后回写；离线时回退到首页 */
self.addEventListener("fetch", function (event) {
  if (event.request.method !== "GET") return;

  event.respondWith(
    caches.match(event.request).then(function (cached) {
      if (cached) return cached;

      return fetch(event.request)
        .then(function (response) {
          // 只缓存同源的正常响应
          if (response.ok && response.type === "basic") {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then(function (cache) {
              cache.put(event.request, copy);
            });
          }
          return response;
        })
        .catch(function () {
          // 离线且未命中缓存时，至少给出首页
          return caches.match("./index.html");
        });
    })
  );
});
