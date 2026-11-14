// Execute the scraper-management (搭积木) helpers from production app.js in a VM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../movsp/web/assets/app.js'), 'utf8');

// Pull the self-contained block from the scraper section start to the api() helper.
const sectionLine = source.indexOf('// 刮削器（搭积木）管理');
const start = source.lastIndexOf('// ----', sectionLine);
const end = source.indexOf('const apiRequests = new Map();');
const presetStart = source.indexOf('function crawlerConfigMarkup(');
assert.ok(start > 0 && sectionLine > start && end > sectionLine && presetStart > 0 && presetStart < start, 'scraper sections must exist in app.js');
// Preset-tab helpers live just above the scraper-management section.
const block = source.slice(presetStart, start) + source.slice(start, end);

function makeContext(scrapers = [], extra = {}) {
  const hosts = new Map();
  const context = vm.createContext({
    state: { scrapers, scraperFilter: 'all', scraperSearch: '', crawlerSources: [], disabledBuiltInCrawlers: [], presetMode: 'form' },
    escapeHtml: value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch])),
    api: async () => ({ scrapers, status: {} }),
    renderConfigFields() {}, showToast() {}, refreshCrawlerSelectionForMediaTypes() {},
    mediaTypesFromForm: () => [{ id: 'normal', name: '普通影片', identifier_kind: 'dvdid', detector: 'fallback' }],
    $: selector => hosts.get(selector) || { innerHTML: '', textContent: '' },
    document: { addEventListener() {}, querySelectorAll: () => [], querySelector: () => null },
    CRAWLER_IDS: ['airav', 'javbus', 'javdb'], BUILT_IN_MEDIA_TYPE_IDS: new Set(['normal']),
    ...extra,
  });
  vm.runInContext(block, context);
  return context;
}

function verifyScraperCardMarkup() {
  const scrapers = [
    { name: 'javbus', display_name: 'JavBus', kind: 'built_in', type: 'site', provides: ['movie_by_code'], fields: [], sample: 'SSIS-001', sample_capability: 'movie_by_code', description: 'JavBus 资料', enabled: true, settings_schema: [], settings: {}, status: { state: 'ok', message: '资料解析正常', http_status: 200, elapsed_ms: 1754, checked_at: '2026-11-06T10:00:00' }, editable_source: false },
    { name: 'tmdb', display_name: 'TMDb（API）', kind: 'plugin', type: 'api', provides: ['movie_by_title', 'tv_by_title'], fields: [], sample: '流浪地球', sample_capability: 'movie_by_title', description: 'TMDb API', enabled: false, settings_schema: [{ key: 'api_key', label: 'API Key', required: true, type: 'password' }], settings: {}, status: { state: 'http_error', http_status: 403, message: '访问被拒绝' }, editable_source: true },
    { name: 'my_scraper', display_name: '我的积木', kind: 'custom', type: 'site', provides: [], fields: [], sample: '', description: '自定义', enabled: true, settings_schema: [], settings: {}, status: { state: 'unknown', message: '尚未检测' }, editable_source: true },
  ];
  const ctx = makeContext(scrapers);
  const html = ctx.scraperCardMarkup(scrapers[0]);
  assert.match(html, /data-scraper-toggle="javbus"/, 'renders a toggle');
  assert.match(html, /checked/, 'enabled scraper switch is on');
  assert.match(html, /scraper-status-ok/, 'shows OK status dot');
  assert.doesNotMatch(html, /scraper-cap-badge/, 'management card no longer shows capability badges');
  assert.match(html, /HTTP 200/, 'shows http status');
  assert.match(html, /1754 ms/, 'shows elapsed time');
  assert.match(html, /data-scraper-test="javbus"/, 'has a test button');
  assert.doesNotMatch(html, /data-scraper-source="javbus"/, 'built-in source is not editable in card');
  assert.doesNotMatch(html, /crawler-config-group|推荐分类/, 'no media-type grouping leaks into the card');

  const plugin = ctx.scraperCardMarkup(scrapers[1]);
  assert.match(plugin, /type-api/, 'API badge');
  assert.match(plugin, /待配置/, 'required-setting hint');
  assert.match(plugin, /disabled/, 'disabled class');
  assert.doesNotMatch(plugin, /checked/, 'disabled switch is off');
  assert.match(plugin, /data-scraper-source="tmdb"/, 'plugin source editable');
  assert.match(plugin, /访问被拒绝/, 'http_error 403 is translated, not shown as raw error');
  assert.doesNotMatch(plugin, /scraper-status-error/, 'no legacy error class');

  const custom = ctx.scraperCardMarkup(scrapers[2]);
  assert.match(custom, /data-scraper-delete="my_scraper"/, 'custom scraper can be deleted');
  assert.match(custom, /data-scraper-test="my_scraper"/, 'detect button is always available');
  console.log('✔ scraper cards render toggles and fine-grained status');
}

function verifyFilterAndSearch() {
  const scrapers = [
    { name: 'a', display_name: 'Alpha', type: 'site', enabled: true, description: '站点', provides: ['movie_by_code'], fields: [], sample: 'SSIS-001', settings: {}, status: {}, settings_schema: [], kind: 'built_in' },
    { name: 'b', display_name: 'Beta', type: 'api', enabled: false, description: 'API', provides: ['movie_by_title'], fields: [], sample: '流浪地球', settings: {}, status: {}, settings_schema: [], kind: 'plugin' },
  ];
  const ctx = makeContext(scrapers);
  assert.match(ctx.scraperGridMarkup(), /data-scraper-card="a"/);
  assert.match(ctx.scraperGridMarkup(), /data-scraper-card="b"/);
  ctx.state.scraperFilter = 'api';
  let html = ctx.scraperGridMarkup();
  assert.doesNotMatch(html, /data-scraper-card="a"/, 'api filter hides site scraper');
  assert.match(html, /data-scraper-card="b"/);
  ctx.state.scraperFilter = 'enabled';
  html = ctx.scraperGridMarkup();
  assert.match(html, /data-scraper-card="a"/);
  assert.doesNotMatch(html, /data-scraper-card="b"/, 'enabled filter hides disabled');
  ctx.state.scraperFilter = 'all';
  ctx.state.scraperSearch = 'beta';
  html = ctx.scraperGridMarkup();
  assert.match(html, /data-scraper-card="b"/);
  assert.doesNotMatch(html, /data-scraper-card="a"/, 'search by display name');
  console.log('✔ scraper filter and search narrow the grid');
}

function verifyCapabilitySections() {
  // 管理页现在是单一扁平网格：只负责显示 + 状态，不再按能力分区
  const scrapers = [
    { name: 'javbus', display_name: 'JavBus', type: 'site', enabled: true, description: 'x', provides: ['movie_by_code'], fields: [], sample: 'SSIS-001', settings: {}, status: {}, settings_schema: [], kind: 'built_in' },
    { name: 'tmdb', display_name: 'TMDb', type: 'api', enabled: true, description: 'y', provides: ['movie_by_title'], fields: [], sample: '流浪地球', settings: {}, status: {}, settings_schema: [], kind: 'plugin' },
    { name: 'avwiki', display_name: 'AVWiki', type: 'site', enabled: true, description: 'z', provides: ['actress_by_name'], fields: [], sample: '三上悠亜', settings: {}, status: {}, settings_schema: [], kind: 'built_in' },
  ];
  const ctx = makeContext(scrapers);
  const html = ctx.scraperGridMarkup();
  assert.match(html, /<div class="scraper-grid">/, 'renders one flat grid');
  assert.doesNotMatch(html, /data-scraper-section/, 'no capability sections in the management page');
  assert.doesNotMatch(html, /番号刮削|标题刮削|演员信息/, 'no capability grouping labels');
  // 容器本身不能也带 scraper-grid，否则会套两层网格、永远只有一列
  const shell = fs.readFileSync(path.join(__dirname, '../movsp/web/index.html'), 'utf8');
  assert.doesNotMatch(shell, /id="crawler-config-content"[^>]*class="[^"]*scraper-grid/,
    'the host element must not be a grid itself');
  assert.match(html, /data-scraper-card="javbus"/);
  assert.match(html, /data-scraper-card="tmdb"/);
  assert.match(html, /data-scraper-card="avwiki"/);
  console.log('✔ management page is a single flat grid with no capability sections');
}

function verifyPresetPickerIsFlat() {
  const scrapers = [
    { name: 'javbus', display_name: 'JavBus', enabled: true, provides: ['movie_by_code'], type: 'site' },
    { name: 'tmdb', display_name: 'TMDb（API）', enabled: true, provides: ['movie_by_title'], type: 'api' },
  ];
  const ctx = makeContext(scrapers, {
    state: { scrapers, crawlerSources: [], disabledBuiltInCrawlers: [], scraperFilter: 'all', scraperSearch: '' },
  });
  // 默认空
  const empty = ctx.crawlerConfigTagsMarkup([]);
  assert.match(empty, /还没有添加刮削器/, 'empty state explains what to do');
  assert.match(empty, /data-crawler-pick="javbus"/, 'offers every enabled scraper');
  assert.match(empty, /data-crawler-pick="tmdb"/);
  assert.doesNotMatch(empty, /crawler-config-group|data-crawler-group/, 'no per-category groups');

  // 已选三个：序号 + 上下移
  const filled = ctx.crawlerConfigTagsMarkup(['javbus', 'tmdb']);
  assert.match(filled, /data-crawler-value="javbus"/);
  assert.match(filled, /class="crawler-tag-order">1</);
  assert.match(filled, /class="crawler-tag-order">2</);
  assert.match(filled, /data-crawler-move="down"/);
  assert.doesNotMatch(filled, /data-crawler-pick="javbus"/, 'already selected is not offered again');
  assert.match(filled, /所有已启用的刮削器都已加入/, 'tells the user nothing is left to add');

  // 旧的分组结构会被压平
  const legacy = ctx.crawlerConfigTagsMarkup({ normal: ['javbus'], fc2: ['tmdb'] });
  assert.match(legacy, /data-crawler-value="javbus"/);
  assert.match(legacy, /data-crawler-value="tmdb"/);
  console.log('✔ preset scraper picker is one flat ordered list, empty by default');
}

function verifyStatusTranslation() {
  const ctx = makeContext([]);
  const cases = [
    [{ state: 'ok' }, '可用'],
    [{ state: 'not_found' }, '未获取到测试数据'],
    [{ state: 'invalid_response' }, '数据解析失败或不完整'],
    [{ state: 'http_error', http_status: 401 }, '需要认证'],
    [{ state: 'http_error', http_status: 403 }, '访问被拒绝'],
    [{ state: 'http_error', http_status: 407 }, '代理需要认证'],
    [{ state: 'http_error', http_status: 429 }, '请求过于频繁'],
    [{ state: 'timeout' }, '连接超时'],
    [{ state: 'dns_error' }, '域名解析失败'],
    [{ state: 'tls_error' }, '证书验证失败'],
    [{ state: 'network_error' }, '网络或代理连接失败'],
    [{ state: 'unknown' }, '未检测'],
  ];
  for (const [status, expected] of cases) {
    assert.equal(ctx.scraperStatusLabel(status), expected, JSON.stringify(status));
  }
  assert.equal(ctx.scraperStatusTone('ok'), 'ok');
  assert.equal(ctx.scraperStatusTone('not_found'), 'warn');
  assert.equal(ctx.scraperStatusTone('dns_error'), 'bad');
  assert.equal(ctx.scraperStatusTone('unknown'), 'idle');
  console.log('✔ every status maps to a Chinese label and a colour tone');
}

function verifyFriendlyNamesInPresetTab() {
  const scrapers = [
    { name: 'tmdb', display_name: 'TMDb（API）', enabled: true, provides: ['movie_by_title'], type: 'api' },
    { name: 'airav', display_name: 'AirAV', enabled: true, provides: ['movie_by_code'], type: 'site' },
    { name: 'javdb', display_name: 'JavDB', enabled: false, provides: ['movie_by_code'], type: 'site' },
  ];
  const ctx = makeContext(scrapers, {
    state: { scrapers, crawlerSources: [], disabledBuiltInCrawlers: ['javdb'], scraperFilter: 'all', scraperSearch: '' },
  });
  const options = ctx.enabledScraperOptions();
  assert.deepEqual(options.map(item => item.name), ['tmdb', 'airav'], 'only enabled scrapers are offered');
  assert.equal(ctx.resolveScraperName('TMDb（API）'), 'tmdb', 'display name resolves to module name');
  assert.equal(ctx.resolveScraperName('tmdb'), 'tmdb');
  const html = ctx.crawlerConfigTagsMarkup({ normal: ['tmdb'] });
  assert.match(html, /crawler-tag-label">TMDb（API）/, 'tag shows friendly label');
  assert.match(html, /data-crawler-value="tmdb"/, 'tag keeps module id for config');
  console.log('✔ preset crawler tab uses friendly names and enabled-only options');
}

async function main() {
  verifyScraperCardMarkup();
  verifyFilterAndSearch();
  verifyCapabilitySections();
  verifyPresetPickerIsFlat();
  verifyStatusTranslation();
  verifyFriendlyNamesInPresetTab();
}
main().catch(error => { console.error(error); process.exit(1); });
