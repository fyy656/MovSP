const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../movsp/web/assets/app.js'), 'utf8');
const functions = source.slice(source.indexOf('function playButton'), source.indexOf('function artworkPlaceholder'));

async function main() {
  let destination, closed = false, message, request;
  const player = { opener: {}, location: { replace: url => { destination = url; } }, close: () => { closed = true; } };
  const button = { dataset: { playTask: 'task/1' }, disabled: false };
  const context = vm.createContext({
    window: { open: () => player },
    api: async url => { request = url; return { links: [{ url: 'https://player.test/web/#/details?id=1' }] }; },
    showToast: error => { message = error; }, escapeHtml: value => value,
  });
  vm.runInContext(functions, context);
  await context.playTask(button);
  assert.equal(request, '/api/tasks/task%2F1/playback');
  assert.equal(destination, 'https://player.test/web/#/details?id=1');
  assert.equal(player.opener, null);
  assert.equal(button.disabled, false);
  context.api = async () => { throw new Error('未找到影片'); };
  await context.playTask(button);
  assert.equal(closed, true);
  assert.equal(message, '未找到影片');
  assert.equal(button.disabled, false);
  let options = { innerHTML: '' }, shown = false, inserted = false;
  const dialog = { querySelector: () => options, showModal: () => { shown = true; } };
  context.$ = () => inserted ? dialog : null;
  context.document = { body: { insertAdjacentHTML: () => { inserted = true; } } };
  context.api = async () => ({ links: [
    { name: 'Jellyfin', url: 'https://one.test/web/' },
    { name: 'Emby', url: 'https://two.test/web/' },
    { name: 'Invalid', url: 'javascript:invalid' },
  ] });
  closed = false;
  await context.playTask(button);
  assert.equal(closed, true);
  assert.equal(shown, true);
  assert.match(options.innerHTML, /https:\/\/one.test\/web\//);
  assert.match(options.innerHTML, /https:\/\/two.test\/web\//);
  assert.match(options.innerHTML, /rel="noopener noreferrer"/);
  assert.doesNotMatch(options.innerHTML, /javascript:/);
  context.window.open = () => null;
  shown = false;
  context.api = async () => ({ links: [{ name: 'Jellyfin', url: 'https://one.test/web/' }] });
  await context.playTask(button);
  assert.equal(shown, true);
  assert.equal(button.disabled, false);

  // 切集时播放器不能消失（只有声音的 bug）：
  // 对话框已开着时必须保留它，且不能做会重渲染影片墙的延迟进度刷新。
  // 播放器代码在 library.js，不在 app.js。
  const librarySource = fs.readFileSync(
    require('node:path').join(__dirname, '../movsp/web/assets/library.js'), 'utf8');
  const playerStart = librarySource.indexOf('function openPlayer(');
  const destroyStart = librarySource.indexOf('function destroyPlayer(');
  const playerEnd = librarySource.indexOf('async function playFilm(');
  assert.ok(playerStart > 0 && playerEnd > playerStart, '应能找到 openPlayer 边界');
  const openPlayerSrc = librarySource.slice(playerStart, playerEnd);
  assert.match(openPlayerSrc, /alreadyOpen/, '应判断对话框是否已打开');
  assert.match(openPlayerSrc, /keepDialog:\s*alreadyOpen/, '已打开时保留对话框');
  assert.match(openPlayerSrc, /skipProgressRefresh:\s*alreadyOpen/,
    '切集时跳过延迟进度刷新（会重渲染影片墙把播放器清掉）');

  const destroySrc = librarySource.slice(destroyStart, destroyStart + 2500);
  assert.match(destroySrc, /skipProgressRefresh\s*=\s*false/, 'destroyPlayer 应支持跳过进度刷新');
  assert.match(destroySrc, /!keepDialog && !skipProgressRefresh/, '两个条件都要判断');

  console.log('Library playback regression checks passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
