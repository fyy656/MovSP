// Cloudflare Worker 图片代理的路径解析与安全限制。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const srcPath = path.join(__dirname, '../docs/cloudflare/movsp-image-proxy.js');
const source = fs.readFileSync(srcPath, 'utf8');

// Worker 用 ESM 的 export default，转成可动态 import 的临时文件
const tmp = path.join(os.tmpdir(), `movsp-worker-${process.pid}.mjs`);
fs.writeFileSync(tmp, source, 'utf8');

(async () => {
  const mod = await import('file:///' + tmp.replace(/\\/g, '/'));
  const worker = mod.default;

  const store = new Map();
  globalThis.caches = {
    default: {
      async match(req) { return store.get(req.url); },
      async put(req, res) { store.set(req.url, res); },
    },
  };
  let lastUrl = '';
  globalThis.fetch = async (url) => {
    lastUrl = String(url);
    if (lastUrl.includes('missing')) return new Response('nf', { status: 404 });
    return new Response('img', {
      status: 200,
      headers: { 'content-type': 'image/jpeg', 'content-length': '3' },
    });
  };

  const ctx = { waitUntil() {} };
  const call = (p, method = 'GET') =>
    worker.fetch(new Request('https://w.dev' + p, { method }), {}, ctx);

  // --- 路径兼容：三种写法都能落到正确的 TMDb 地址 ---
  const paths = [
    ['/t/p/w500/a.jpg', 'https://image.tmdb.org/t/p/w500/a.jpg'],
    ['/w500/b.jpg', 'https://image.tmdb.org/t/p/w500/b.jpg'],
    ['/image.tmdb.org/t/p/w1280/c.jpg', 'https://image.tmdb.org/t/p/w1280/c.jpg'],
  ];
  for (const [given, expect] of paths) {
    const res = await call(given);
    assert.equal(res.status, 200, `${given} 应成功`);
    assert.equal(lastUrl, expect, `${given} 应回源到 ${expect}`);
  }

  // --- 安全：只放行白名单域名（其它域名被当成路径，不会成为回源目标）---
  const evil = await call('/evil.com/t/p/w500/a.jpg');
  assert.notEqual(evil.status, 200, '非白名单域名不应被代理');
  assert.ok(!lastUrl.includes('evil.com'), '绝不能回源到非白名单域名');

  // --- 安全：尺寸白名单 ---
  let res = await call('/t/p/w999/a.jpg');
  assert.equal(res.status, 400, '不支持的尺寸应拒绝');

  // --- 安全：路径穿越 ---
  res = await call('/t/p/w500/../etc/passwd');
  assert.equal(res.status, 400, '路径穿越应拒绝');

  // --- 错误透传 ---
  res = await call('/t/p/w500/missing.jpg');
  assert.equal(res.status, 404, '上游 404 应透传');

  // --- 方法限制 ---
  res = await call('/t/p/w500/a.jpg', 'POST');
  assert.equal(res.status, 405, '只允许 GET/HEAD');

  // --- 缓存：第二次请求不应再回源 ---
  lastUrl = '';
  const cached = await call('/t/p/w500/a.jpg');
  assert.equal(cached.status, 200);
  assert.equal(lastUrl, '', '命中边缘缓存后不应回源');

  // --- 源码约束 ---
  assert.match(source, /ALLOWED_HOSTS/, '应有域名白名单');
  assert.match(source, /ALLOWED_SIZES/, '应有尺寸白名单');
  assert.doesNotMatch(source, /eval\(|new Function/, '不应使用动态执行');

  fs.unlinkSync(tmp);
  console.log('✔ image proxy worker: paths, whitelist, traversal, cache');
})().catch((error) => {
  try { fs.unlinkSync(tmp); } catch (_) { /* 忽略 */ }
  console.error(error.message);
  process.exit(1);
});
