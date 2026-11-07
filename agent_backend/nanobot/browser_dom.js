function auraDOM(payload) {
  'use strict';
  const {mode, args, key} = payload;
  const state = globalThis[key] ??= {document, url: location.href, nodes: new Map(), identities: new WeakMap(), next: 0};
  if (state.document !== document || state.url !== location.href) {
    globalThis[key] = {document, url: location.href, nodes: new Map(), identities: new WeakMap(), next: 0};
    return {error: 'BROWSER_STALE_DOCUMENT'};
  }
  const compact = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const privateControl = el => el instanceof HTMLInputElement &&
    (el.type === 'password' || /(?:password|one-time-code|cc-number|cc-csc)/i.test(el.autocomplete));
  const visible = el => {
    if (!(el instanceof Element) || privateControl(el) || ['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) return false;
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const label = el => compact(el.getAttribute('aria-label') ||
    (el.labels && [...el.labels].map(x => x.innerText).join(' ')) ||
    el.getAttribute('placeholder') || el.innerText || el.getAttribute('title') || el.getAttribute('alt') || el.name || '').slice(0,400);
  const identity = el => JSON.stringify({tag: el.tagName, type: String(el.type || '').slice(0,64), id: String(el.id || '').slice(0,256),
    name: (el.getAttribute('name') || '').slice(0,256), role: (el.getAttribute('role') || '').slice(0,128), label: label(el),
    href: el instanceof HTMLAnchorElement ? el.href.slice(0,4096) : ''});
  const details = el => ({tag: el.tagName.toLowerCase(), type: el.type || null, role: el.getAttribute('role'),
    id:String(el.id || '').slice(0,256),name:(el.getAttribute('name') || '').slice(0,256),
    label: label(el), disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    value: !privateControl(el) && ('value' in el) && el.type !== 'file' ? String(el.value).slice(0,16384) : undefined,
    checked: 'checked' in el ? Boolean(el.checked) : undefined,
    expanded: el.getAttribute('aria-expanded'),
    href: el instanceof HTMLAnchorElement ? el.href.slice(0,4096) : undefined,
    options: el instanceof HTMLSelectElement ? [...el.options].slice(0,100).map(o => ({value:o.value.slice(0,1000),label:compact(o.label).slice(0,400),selected:o.selected})) : undefined});
  const remember = el => {
    let node = state.identities.get(el);
    if (!node) { node = String(++state.next); state.identities.set(el,node); }
    state.nodes.set(node,el);
    if (state.nodes.size > 2500) state.nodes.delete(state.nodes.keys().next().value);
    return {node, fingerprint: identity(el), ...details(el)};
  };
  if (mode === 'observe') {
    let elements = [], scanned = 0, scanLimited = false;
    const query = args.query || '';
    const selector = query.startsWith('text:') || !query ? 'a,button,input,textarea,select,[role],[contenteditable="true"],[tabindex]' : query;
    try {
      const roots = [document];
      for (let index=0;index<roots.length && index<100;index++) {
        const root = roots[index];
        for (const element of root.querySelectorAll(selector)) {
          if (elements.length>=5000) { scanLimited=true; break; }
          elements.push(element);
        }
        for (const element of root.querySelectorAll('*')) {
          if (++scanned>20000) { scanLimited=true; break; }
          if (element.shadowRoot) roots.push(element.shadowRoot);
        }
        if (scanLimited) break;
      }
    } catch { return {error:'BROWSER_INVALID_QUERY'}; }
    elements = [...new Set(elements)].filter(visible).filter(el => el.type !== 'file');
    if (query.startsWith('text:')) {
      const needle = query.slice(5).toLocaleLowerCase();
      elements = elements.filter(el => label(el).toLocaleLowerCase().includes(needle));
    }
    const total = elements.length;
    elements = elements.slice(args.offset,args.offset+args.limit).map(remember);
    const body = document.body ? document.body.innerText : '';
    const textOffset = args.text_offset || 0, textLimit = args.text_limit || 12000;
    const links = [...document.links].slice(0,5000).filter(visible).slice(args.offset,args.offset+args.limit)
      .map(el => ({text:label(el),url:el.href.slice(0,4096)}));
    return {title:document.title,url:location.href,ready_state:document.readyState,
      elements,total_elements:total,offset:args.offset,next_offset:Math.min(args.offset+args.limit,total),
      truncated:args.offset+args.limit<total,links,text:body.slice(textOffset,textOffset+textLimit),
      text_offset:textOffset,total_text_characters:body.length,next_text_offset:Math.min(textOffset+textLimit,body.length),
      text_truncated:textOffset+textLimit<body.length,frame_count:document.querySelectorAll('iframe').length,
      element_scan_limited:scanLimited,scope:'top document and open shadow DOM; cross-origin iframe contents are not exported'};
  }
  const element = state.nodes.get(String(args.node));
  if (!element || !element.isConnected || !visible(element) || identity(element) !== args.fingerprint) return {error:'BROWSER_STALE_REFERENCE'};
  if (privateControl(element) || element.type === 'file') return {error:'BROWSER_PRIVATE_CONTROL'};
  if (mode === 'inspect') return {state:details(element),node:String(args.node),fingerprint:identity(element)};
  if (mode !== 'act') return {error:'BROWSER_INVALID_OPERATION'};
  if (element.disabled || element.getAttribute('aria-disabled') === 'true') return {error:'BROWSER_CONTROL_DISABLED'};
  const before = details(element), action = args.action, value = args.value;
  let changed = false, dispatched = false;
  if (action === 'fill') {
    if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
      if (element.readOnly || /^(checkbox|radio|button|submit|reset|hidden)$/i.test(element.type || '')) return {error:'BROWSER_CONTROL_NOT_EDITABLE'};
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype,'value').set;
      if (element.value !== value) {
        setter.call(element,value);
        element.dispatchEvent(new Event('input',{bubbles:true}));
        element.dispatchEvent(new Event('change',{bubbles:true}));
        changed = true;
      }
    } else if (element.isContentEditable) {
      if (element.innerText !== value) {
        element.textContent = value;
        element.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));
        changed = true;
      }
    } else return {error:'BROWSER_CONTROL_NOT_EDITABLE'};
  } else if (action === 'select') {
    if (!(element instanceof HTMLSelectElement)) return {error:'BROWSER_CONTROL_NOT_SELECT'};
    const options = [...element.options].filter(o => o.value === value || compact(o.label) === value);
    if (options.length !== 1) return {error:'BROWSER_OPTION_AMBIGUOUS_OR_MISSING'};
    if (element.value !== options[0].value) {
      element.value=options[0].value;
      element.dispatchEvent(new Event('input',{bubbles:true}));
      element.dispatchEvent(new Event('change',{bubbles:true}));
      changed = true;
    }
  } else if (action === 'check' || action === 'uncheck') {
    if (!(element instanceof HTMLInputElement) || !['checkbox','radio'].includes(element.type)) return {error:'BROWSER_CONTROL_NOT_CHECKABLE'};
    const desired = action === 'check';
    if (element.type === 'radio' && !desired) return {error:'BROWSER_RADIO_UNCHECK_UNSUPPORTED'};
    if (element.checked !== desired) { HTMLElement.prototype.click.call(element); changed = true; }
  } else if (action === 'click') {
    HTMLElement.prototype.click.call(element); dispatched = true;
  } else if (action === 'focus' || action === 'press') {
    const wasFocused = document.activeElement === element;
    element.focus({preventScroll:false});
    changed = !wasFocused && document.activeElement === element;
  } else if (action === 'scroll') {
    const vector = {down:[0,400],up:[0,-400],left:[-400,0],right:[400,0]}[value];
    if (!vector) return {error:'BROWSER_INVALID_SCROLL_DIRECTION'};
    const x=element.scrollLeft,y=element.scrollTop;
    element.scrollBy({left:vector[0],top:vector[1],behavior:'instant'});
    changed=x!==element.scrollLeft || y!==element.scrollTop;
  } else if (action === 'expand' || action === 'collapse') {
    const desired=action==='expand';
    if (element instanceof HTMLDetailsElement) {
      changed=element.open!==desired;element.open=desired;
    } else if (element.hasAttribute('aria-expanded')) {
      if ((element.getAttribute('aria-expanded')==='true')!==desired) { HTMLElement.prototype.click.call(element);changed=true; }
    } else return {error:'BROWSER_CONTROL_NOT_EXPANDABLE'};
  } else return {error:'BROWSER_UNSUPPORTED_ACTION'};
  const after = element.isConnected ? details(element) : null;
  let verified = false;
  if (action==='fill') verified=after && (element.isContentEditable ? element.innerText===value : after.value===value);
  if (action==='select') verified=after && element.value===([...element.options].find(o => o.value===value || compact(o.label)===value)||{}).value;
  if (action==='check' || action==='uncheck') verified=after && after.checked===(action==='check');
  if (action==='focus') verified=document.activeElement===element;
  if (action==='expand' || action==='collapse') verified=element instanceof HTMLDetailsElement ? element.open===(action==='expand') : (element.getAttribute('aria-expanded')==='true')===(action==='expand');
  if (action==='scroll') verified=changed;
  return {before,after,changed,property_verified:verified,dispatched:dispatched || changed || action==='press',
    scope:'Control/property observation only; click/press dispatch is not proof that a remote business goal succeeded.'};
}
