import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import vm from 'node:vm';
import crypto from 'node:crypto';
import test from 'node:test';

const folder = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../browser-extension/whale-ask');
const source = name => readFileSync(path.join(folder, name), 'utf8');

function worker(options = {}) {
  const sent = [], failures = [], menu = [];
  const event = () => ({addListener() {}});
  const chrome = {
    contextMenus: {removeAll(callback) { callback(); }, create(value, callback) { menu.push(value); callback(); }, onClicked: event()},
    scripting: {async executeScript(value) {
      assert.equal(value.world, 'ISOLATED');
      assert.equal(typeof value.func, 'function');
      if (options.extractError) throw new Error('restricted page');
      return [{result: options.extracted || {title: '示例标题', page_text: '页面介绍了 Python 环境修复。'}}];
    }},
    runtime: {
      onInstalled: event(), onStartup: event(), lastError: null,
      getURL(value) { return `chrome-extension://test/${value}`; },
      sendNativeMessage(host, request, callback) {
        sent.push({host, request});
        if (options.nativeError) chrome.runtime.lastError = {message: options.nativeError};
        callback(options.response === undefined ? {ok: true} : options.response);
        chrome.runtime.lastError = null;
      },
    },
    tabs: {create(value) { failures.push(value); }},
  };
  const context = vm.createContext({chrome, URL, TextEncoder, navigator: {userAgent: 'Edg/144.0'}, Date});
  context.importScripts = name => vm.runInContext(source(name), context);
  vm.runInContext(source('service_worker.js'), context);
  const info = {menuItemId: 'aura-ask-selection', selectionText: '这个报错是什么意思？', pageUrl: 'https://example.test/article?token=secret#private', editable: false};
  const tab = {id: 5, title: 'example', url: info.pageUrl};
  return {sent, failures, menu, context, info, tab,
    run: (value = info, currentTab = tab) => context.askSelection(value, currentTab)};
}

test('manifest is least privilege, stable ID, packaged scripts and real whale icon', () => {
  const manifest = JSON.parse(source('manifest.json'));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions.sort(), ['activeTab', 'contextMenus', 'nativeMessaging', 'scripting'].sort());
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.content_scripts, undefined);
  const key = Buffer.from(manifest.key, 'base64');
  const id = crypto.createHash('sha256').update(key).digest('hex').slice(0, 32).split('').map(n => String.fromCharCode(97 + Number.parseInt(n, 16))).join('');
  assert.equal(id, 'gbeikpndpckhjeakbckijelidpdpinei');
  for (const n of [16, 48, 128]) {
    const png = readFileSync(path.join(folder, manifest.icons[String(n)]));
    assert.equal(png.toString('hex', 0, 8), '89504e470d0a1a0a');
    assert.equal(png.readUInt32BE(16), n);
    assert.equal(png.readUInt32BE(20), n);
  }
  assert.match(manifest.content_security_policy.extension_pages, /connect-src 'none'/);
});

test('one native selection menu uses packaged whale icon via manifest', () => {
  const harness = worker();
  harness.context.installMenu();
  assert.equal(harness.menu.length, 1);
  assert.equal(harness.menu[0].title, '问问鲸鱼娘');
  assert.equal(JSON.stringify(harness.menu[0].contexts), '["selection"]');
  assert.equal(JSON.stringify(harness.menu[0].documentUrlPatterns), '["http://*/*","https://*/*"]');
});

test('click sends selected text and bounded page context once without sensitive URL query', async () => {
  const harness = worker();
  await harness.run();
  assert.equal(harness.sent.length, 1);
  const {host, request} = harness.sent[0];
  assert.equal(host, 'aura.whale_ask');
  assert.equal(request.version, 1);
  assert.equal(request.action, 'ask_selection');
  assert.equal(request.context.text, harness.info.selectionText);
  assert.equal(request.context.url, 'https://example.test/article');
  assert.equal(request.context.title, '示例标题');
  assert.match(request.context.page_text, /Python/);
  assert.equal(request.context.app_name, 'Microsoft Edge');
  assert.ok(Number.isFinite(Date.parse(request.context.captured_at)));
  assert.equal(harness.failures.length, 0);
  await harness.run({...harness.info, selectionText: '    if ready:\n        run()\n'});
  assert.equal(harness.sent[1].request.context.text, '    if ready:\n        run()\n');
});

test('native bridge failure opens a visible local page without putting selections in URL', async () => {
  const harness = worker({nativeError: 'Specified native messaging host not found.'});
  await harness.run();
  assert.equal(harness.failures.length, 1);
  const url = harness.failures[0].url;
  assert.match(url, /^chrome-extension:\/\/test\/status.html\?error=/);
  assert.match(decodeURIComponent(url), /install_browser_ask/);
  assert.ok(!decodeURIComponent(url).includes(harness.info.selectionText));
  assert.ok(!url.includes('secret'));
});

test('negative native ACK is visible and does not pretend the model answered', async () => {
  const harness = worker({response: {ok: false, error: '桌宠正在退出，请稍后重试。'}});
  await harness.run();
  assert.match(decodeURIComponent(harness.failures[0].url), /桌宠正在退出/);
});

test('restricted injection degrades to selection with an explicit note', async () => {
  const harness = worker({extractError: true});
  await harness.run();
  assert.equal(harness.sent[0].request.context.page_text, '');
  assert.match(harness.sent[0].request.context.notes.join(' '), /不允许读取正文/);
});

test('navigation during capture never attaches a different page to the original selection', async () => {
  const harness = worker({extracted: {title: 'Different', page_url: 'https://different.test/', page_text: 'This belongs to another page'}});
  await harness.run();
  assert.equal(harness.sent[0].request.context.page_text, '');
  assert.equal(harness.sent[0].request.context.title, harness.tab.title);
  assert.match(harness.sent[0].request.context.notes.join(' '), /跳转/);
});

test('form selection, empty selection and non-web URL never cross native bridge', async () => {
  for (const patch of [{editable: true}, {selectionText: '  '}, {pageUrl: 'edge://settings'}, {pageUrl: 'https://user:password@example.test/'}]) {
    const harness = worker();
    await harness.run({...harness.info, ...patch});
    assert.equal(harness.sent.length, 0);
    assert.equal(harness.failures.length, 1);
  }
});

test('malicious large text stays inside character and total UTF-8 limits', async () => {
  const harness = worker({extracted: {title: 't'.repeat(900), page_text: '🐋'.repeat(40000), note: 'n'.repeat(900)}});
  await harness.run({...harness.info, selectionText: '鲸'.repeat(40000)});
  const request = harness.sent[0].request;
  assert.equal(request.context.text.length, 8000);
  assert.ok(request.context.page_text.length <= 16000);
  assert.equal(request.context.title.length, 300);
  assert.ok(request.context.notes.every(n => n.length <= 200));
  assert.ok(new TextEncoder().encode(JSON.stringify(request)).byteLength <= 96 * 1024);
});

function extractHarness(specs, selected = null) {
  const body = {nodeType: 1, parentElement: null, matches: () => false};
  const root = {nodeType: 1, parentElement: body, matches: () => false};
  const elements = specs.map(spec => ({
    nodeType: 1, parentElement: root, excluded: !!spec.excluded,
    style: {display: 'block', visibility: 'visible', opacity: '1', ...spec.style},
    matches() { return this.excluded; },
    closest() { return this.excluded ? this : null; },
  }));
  const nodes = specs.map((spec, index) => ({nodeType: 3, nodeValue: spec.text, parentElement: elements[index]}));
  let index = 0;
  const context = vm.createContext({
    document: {title: '正文测试', body, querySelector: () => root, createTreeWalker: () => ({nextNode: () => nodes[index++] || null})},
    window: {getSelection: () => ({anchorNode: selected === null ? null : nodes[selected], focusNode: null}), getComputedStyle: element => element.style || {display: 'block', visibility: 'visible', opacity: '1'}},
    NodeFilter: {SHOW_TEXT: 4},
  });
  vm.runInContext(source('extract.js'), context);
  return {run: limit => context.whaleExtractPage(limit), visited: () => index};
}

test('extractor excludes hidden elements, input/editable/script nodes, keeps readable context', () => {
  const harness = extractHarness([
    {text: '可见的项目说明'}, {text: '密码和脚本不能进入上下文', excluded: true},
    {text: '隐藏的内容', style: {display: 'none'}}, {text: '透明隐藏', style: {opacity: '0'}},
    {text: '   第二段  正文   '},
  ]);
  assert.equal(harness.run(16000).page_text, '可见的项目说明\n第二段 正文');
});

test('selected private/editable node blocks the whole request', () => {
  const harness = extractHarness([{text: 'private draft', excluded: true}], 0);
  assert.equal(harness.run(16000).blocked, true);
});

test('extractor is bounded for huge documents and oversized individual nodes', () => {
  const many = extractHarness(Array.from({length: 9000}, () => ({text: ' ' })));
  many.run(16000);
  assert.ok(many.visited() <= 5001);
  assert.equal(extractHarness([{text: 'x'.repeat(40000)}]).run(16000).page_text.length, 16000);
});

test('status UI renders error as textContent rather than HTML', () => {
  const label = {textContent: '', hidden: true};
  const content = '<img src=x onerror=alert(1)>';
  vm.runInNewContext(source('status.js'), {URLSearchParams, location: {search: `?error=${encodeURIComponent(content)}`}, document: {getElementById: () => label}});
  assert.equal(label.textContent, content);
  assert.equal(label.hidden, false);
});
