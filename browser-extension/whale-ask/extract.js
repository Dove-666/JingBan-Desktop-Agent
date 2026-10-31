/* This self-contained function is injected only after the user clicks our menu. */
function whaleExtractPage(maxCharacters = 16000) {
  const excluded = 'script,style,noscript,template,head,svg,canvas,input,textarea,select,option,' +
    '[contenteditable],[role="textbox"],[hidden],[aria-hidden="true"],[inert],[data-private]';
  const selection = window.getSelection();
  for (const node of [selection?.anchorNode, selection?.focusNode]) {
    const element = node?.nodeType === 1 ? node : node?.parentElement;
    if (element?.closest(excluded)) {
      return {blocked: true, note: '输入框或私密区域中的内容不会发送。'};
    }
  }
  const root = document.querySelector('main,article,[role="main"]') || document.body;
  if (!root) return {page_text: '', note: '当前页面没有可读取的正文，已保留选中内容。'};
  const visible = new WeakMap();
  function isVisible(element) {
    if (!element) return false;
    if (visible.has(element)) return visible.get(element);
    if (element.matches(excluded)) { visible.set(element, false); return false; }
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || Number(style.opacity) === 0) {
      visible.set(element, false);
      return false;
    }
    const allowed = element === document.body ||
      (element.parentElement ? isVisible(element.parentElement) : true);
    visible.set(element, allowed);
    return allowed;
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const chunks = [];
  let count = 0;
  let length = 0;
  let node;
  // The node cap also bounds work for huge or deliberately hostile documents.
  while ((node = walker.nextNode()) && count++ < 5000 && length < maxCharacters) {
    if (!node.parentElement || !isVisible(node.parentElement)) continue;
    const text = String(node.nodeValue || '').replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const part = text.slice(0, Math.max(0, maxCharacters - length));
    chunks.push(part);
    length += part.length + 1;
  }
  return {
    title: String(document.title || '').slice(0, 300),
    page_url: String(window.location?.href || ''),
    page_text: chunks.join('\n').slice(0, maxCharacters),
    note: length >= maxCharacters || count >= 5000 ? '页面较长，仅附带部分正文。' : '',
  };
}
