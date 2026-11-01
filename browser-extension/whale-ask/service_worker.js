/* No remote scripts, fetches, API credentials, persistent page access or screenshots. */
importScripts('extract.js');

const HOST_NAME = 'aura.whale_ask';
const MENU_ID = 'aura-ask-selection';
const MAX_REQUEST_BYTES = 96 * 1024;

function safePageUrl(value) {
  const url = new URL(String(value || ''));
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('目前只支持普通 HTTP/HTTPS 网页，请在网页中选中文字后重试。');
  }
  // Query strings and fragments can contain login tokens or private searches.
  return `${url.origin}${url.pathname}`.slice(0, 2048);
}

function buildAskRequest(info, tab, extracted = {}) {
  if (info.editable || extracted.blocked) {
    throw new Error('为避免发送密码或未提交的表单，输入框和可编辑区域暂不支持。');
  }
  const text = String(info.selectionText || '').slice(0, 8000);
  if (!text.trim()) throw new Error('没有选中可发送的文字，请先选中一段网页文字。');
  const notes = [];
  if (String(info.selectionText || '').length > 8000) notes.push('选中内容较长，仅附带前 8000 个字符。');
  if (extracted.note) notes.push(String(extracted.note).slice(0, 200));
  if (extracted.page_url && new URL(extracted.page_url).href !== new URL(info.frameUrl || info.pageUrl || tab.url).href) {
    extracted = {};
    notes.push('页面已发生跳转，未附带新页面的正文；选中内容仍然保留。');
  }
  const context = {
    text,
    title: String(extracted.title || tab.title || '').slice(0, 300),
    url: safePageUrl(info.pageUrl || tab.url),
    page_text: String(extracted.page_text || '').slice(0, 16000),
    captured_at: new Date().toISOString(),
    app_name: /Edg\//.test(navigator.userAgent) ? 'Microsoft Edge' : 'Google Chrome',
    notes,
  };
  const request = {version: 1, action: 'ask_selection', context};
  const size = () => new TextEncoder().encode(JSON.stringify(request)).byteLength;
  if (size() > MAX_REQUEST_BYTES) {
    context.notes.push('为控制上下文大小，已缩短页面正文。');
    while (size() > MAX_REQUEST_BYTES && context.page_text.length) {
      context.page_text = context.page_text.slice(0, Math.floor(context.page_text.length * 0.8));
    }
  }
  if (size() > MAX_REQUEST_BYTES) throw new Error('选中内容过长，请缩小选择范围后重试。');
  return request;
}

function installMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID, title: '问问鲸鱼娘', contexts: ['selection'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'],
    }, () => { void chrome.runtime.lastError; });
  });
}

function friendlyBridgeError(message) {
  const text = String(message || '');
  if (/not found|not registered|specified native messaging host|找不到|未注册/i.test(text)) {
    return '尚未安装本地连接桥。请先运行项目的 tools/install_browser_ask.ps1 -Apply，再重试。';
  }
  if (/forbidden|access.*denied|not allowed|禁止|拒绝/i.test(text)) {
    return '浏览器没有连接桌宠的权限。请重新安装本地连接桥，并检查扩展 ID 是否匹配。';
  }
  return '暂时无法连接鲸鱼娘。请确认桌宠已启动、本地连接桥已安装，然后重试。';
}

function showFailure(message) {
  // Only static/local error text goes in this URL. Never selections or page text.
  const url = chrome.runtime.getURL(`status.html?error=${encodeURIComponent(String(message).slice(0, 400))}`);
  chrome.tabs.create({url});
}

async function askSelection(info, tab) {
  if (info.menuItemId !== MENU_ID) return;
  try {
    safePageUrl(info.pageUrl || tab?.url);
    if (info.editable) throw new Error('为避免发送密码或未提交的表单，输入框和可编辑区域暂不支持。');
    if (!Number.isInteger(tab?.id)) throw new Error('当前标签页已关闭，请重新选中网页文字。');
    let extracted = {};
    try {
      const target = {tabId: tab.id};
      if (Number.isInteger(info.frameId) && info.frameId > 0) target.frameIds = [info.frameId];
      const results = await chrome.scripting.executeScript({target, world: 'ISOLATED', func: whaleExtractPage, args: [16000]});
      extracted = results?.[0]?.result || {};
    } catch {
      extracted = {note: '当前页不允许读取正文，已保留选中内容。'};
    }
    const request = buildAskRequest(info, tab, extracted);
    chrome.runtime.sendNativeMessage(HOST_NAME, request, (response) => {
      const error = chrome.runtime.lastError;
      if (error) { showFailure(friendlyBridgeError(error.message)); return; }
      if (!response || response.ok !== true) {
        // The bridge returns a bounded human-readable error; do not expose traces.
        showFailure(typeof response?.error === 'string' ? response.error : '桌宠没有确认接收，请确认桌宠正在运行后重试。');
      }
    });
  } catch (error) {
    showFailure(error instanceof Error ? error.message : '无法读取这次选择，请重新选中一段网页文字。');
  }
}

chrome.runtime.onInstalled.addListener(installMenu);
chrome.runtime.onStartup.addListener(installMenu);
chrome.contextMenus.onClicked.addListener((info, tab) => { void askSelection(info, tab); });
