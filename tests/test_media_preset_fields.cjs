// 影视预设不应出现 JAV 专用字段：逐条核对渲染输入。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../movsp/web/assets/app.js'), 'utf8');

function extract(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `找不到 ${startMarker}`);
  const end = source.indexOf(endMarker, start);
  assert.ok(end > start, `找不到 ${endMarker}`);
  return source.slice(start, end);
}

// 1) 常量段：两个隐藏集合 + 页签定义（formTabsFor 在 PRESET_KINDS 之后）
const constBlock = extract('const MEDIA_FORM_TABS', 'const NAMING_RULE_VARIABLES');

// 2) renderConfigFields 函数：过滤逻辑
const fnBlock = extract('function renderConfigFields()', '\nfunction ');

const context = vm.createContext({
  console,
  Set,
  Map,
  Array,
  Object,
  String,
  Number,
  JSON,
  Boolean,
  Math,
});
vm.runInContext(`${constBlock}\nglobalThis.__hoisted = {
  MEDIA_HIDDEN_CRAWLER_FIELDS,
  MEDIA_HIDDEN_NETWORK_FIELDS,
  MEDIA_FORM_TABS,
};`, context);

const hoisted = context.__hoisted;

// --- 断言 1：影视必须隐藏的 JAV 专用字段 ---
const mustHide = [
  'selection',              // 番号爬虫列表
  'required_keys',          // 番号必填校验
  'hardworking',            // 深度抓取
  'respect_site_avid',      // 站点番号优先
  'fc2fan_local_path',      // FC2 镜像
  'sleep_after_scraping',   // 引擎限速
  'use_javdb_cover',        // JavDB 封面
  'normalize_actress_name', // 艺名归一
];
for (const key of mustHide) {
  assert.ok(hoisted.MEDIA_HIDDEN_CRAWLER_FIELDS.has(key), `影视预设应隐藏 crawler.${key}`);
}

// --- 断言 2：这些字段在电影路径下确实不会被读 ---
// （对照 scrapers.py：只读 api_key/base_url/language/image_base）
const heavyFields = ['selection', 'hardworking', 'use_javdb_cover', 'fc2fan_local_path'];
for (const key of heavyFields) {
  assert.ok(!source.includes(`cfg.get("${key}")`), `${key} 不应是 TMDb 插件的配置项`);
}

// --- 断言 3：网络页签隐藏免代理站点（JAV 镜像地址） ---
assert.ok(hoisted.MEDIA_HIDDEN_NETWORK_FIELDS.has('proxy_free'), '影视隐藏免代理站点');

// --- 断言 4：影视保留真正有用的页签与字段 ---
const mediaTabs = hoisted.MEDIA_FORM_TABS.map(t => t.id);
// 注意：vm 里创建的数组来自不同 realm，deepEqual 会因原型不同而失败
assert.equal(JSON.stringify(mediaTabs), JSON.stringify(['scanner', 'crawler', 'network']),
  '影视只保留三个页签');
assert.ok(!mediaTabs.includes('folder'), '影视没有文件夹整理');
assert.ok(!mediaTabs.includes('translator'), '影视没有翻译器');
assert.ok(!mediaTabs.includes('images'), '影视没有封面裁剪页签');

// --- 断言 5：JAV 预设不受影响（集合只在 kind==='media' 时生效） ---
assert.match(fnBlock, /if \(kind === 'media'\)/, '过滤必须限定 media');
assert.match(fnBlock, /MEDIA_HIDDEN_CRAWLER_FIELDS\.has\(path\)/, 'crawler 过滤已接入');
assert.match(fnBlock, /MEDIA_HIDDEN_NETWORK_FIELDS\.has\(path\)/, 'network 过滤已接入');
assert.ok(hoisted.MEDIA_HIDDEN_CRAWLER_FIELDS.size >= 8, '至少隐藏 8 个 JAV 字段');

console.log('✔ media preset hides JAV-only crawler/network fields');
