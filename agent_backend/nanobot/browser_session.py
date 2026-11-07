"""On-demand isolated Edge/Chrome DOM tools; no user profile or arbitrary JS.

CDP is private to the owned process. Model-facing arguments are URLs, CSS/text
queries, opaque references and a fixed action vocabulary. A pinned HTTP proxy
and Fetch interception enforce the same public-target policy as web_fetch.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

try:
    from .browser_network import BrowserNetworkError, PinnedPublicProxy, PublicBrowserPolicy
except ImportError:
    from browser_network import BrowserNetworkError, PinnedPublicProxy, PublicBrowserPolicy

_DOM = Path(__file__).with_name('browser_dom.js')
_ACTIONS = frozenset({'click','fill','select','check','uncheck','focus','press','scroll','expand','collapse'})
_KEYS = {'Enter':13,'Tab':9,'Escape':27,'ArrowDown':40,'ArrowUp':38,'ArrowLeft':37,'ArrowRight':39,'Space':32,'Backspace':8,'Delete':46,'Home':36,'End':35}


class BrowserError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code=code
        self.uncertain=uncertain
        self.effect_state='uncertain' if uncertain else 'not_dispatched'
        self.pending_effect=uncertain
        super().__init__(code)


def find_browser():
    """Known installation executables only; never inspect user browser data."""
    candidates=[]
    for environment in ('PROGRAMFILES(X86)','PROGRAMFILES','LOCALAPPDATA'):
        root=os.environ.get(environment)
        if root:
            candidates += [Path(root)/'Microsoft/Edge/Application/msedge.exe',Path(root)/'Google/Chrome/Application/chrome.exe']
    if os.name!='nt':
        candidates += [Path(path) for name in ('chromium','chromium-browser','google-chrome','microsoft-edge') if (path:=shutil.which(name))]
    return next((path.resolve() for path in candidates if path.is_file()),None)


find_executable=find_browser


class _CDP:
    def __init__(self, websocket, handler, timeout):
        self.websocket=websocket
        self.handler=handler
        self.timeout=timeout
        self.sequence=0
        self.pending={}
        self.events=set()
        self.reader=asyncio.create_task(self._read())

    async def call(self, method, params=None, session=None):
        self.sequence+=1
        identifier=self.sequence
        future=asyncio.get_running_loop().create_future()
        self.pending[identifier]=future
        message={'id':identifier,'method':method,'params':params or {}}
        if session is not None:
            message['sessionId']=session
        try:
            await self.websocket.send(json.dumps(message))
            return await asyncio.wait_for(future,timeout=self.timeout)
        except asyncio.TimeoutError:
            raise BrowserError('BROWSER_COMMAND_TIMEOUT',uncertain=True) from None
        except (OSError,ConnectionError):
            raise BrowserError('BROWSER_CONNECTION_CLOSED',uncertain=True) from None
        finally:
            self.pending.pop(identifier,None)

    async def _read(self):
        try:
            async for raw in self.websocket:
                value=json.loads(raw)
                if 'id' in value:
                    future=self.pending.get(value['id'])
                    if future is not None and not future.done():
                        if 'error' in value:
                            future.set_exception(BrowserError('BROWSER_PROTOCOL_COMMAND_FAILED'))
                        else:
                            future.set_result(value.get('result',{}))
                elif 'method' in value:
                    task=asyncio.create_task(self.handler(value))
                    self.events.add(task)
                    def finished(result):
                        self.events.discard(result)
                        if not result.cancelled():
                            with suppress(Exception): result.result()
                    task.add_done_callback(finished)
        except (Exception,asyncio.CancelledError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(BrowserError('BROWSER_CONNECTION_CLOSED',uncertain=True))

    async def close(self):
        with suppress(Exception): await self.websocket.close()
        if self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader,return_exceptions=True)
        for task in list(self.events): task.cancel()
        if self.events: await asyncio.gather(*self.events,return_exceptions=True)


class BrowserSession:
    def __init__(self, *, executable=None, allowed_test_hosts=(), timeout=15, headless=True):
        self.executable=Path(executable).resolve() if executable is not None else find_browser()
        self.timeout=max(3,min(float(timeout),30))
        self.headless=bool(headless)
        self.policy=PublicBrowserPolicy(allowed_test_hosts=allowed_test_hosts)
        self.proxy=PinnedPublicProxy(self.policy)
        self._process=None
        self._profile=None
        self._connection=None
        self._tabs={}
        self._sessions={}
        self._active=None
        self._references={}
        self._results={}
        self._uncertain=set()
        self._pending_network={}
        self._blocked=[]
        self._start_lock=asyncio.Lock()
        self._starting_task=None
        self._operation_lock=asyncio.Lock()
        self._closed=False
        self._close_lock=asyncio.Lock()
        self._close_complete=False
        self._dom_key='__aura_'+secrets.token_hex(16)

    @staticmethod
    def availability():
        return {'available':find_browser() is not None,'reason_code':'' if find_browser() is not None else 'BROWSER_NOT_INSTALLED',
                'scope':'isolated anonymous browser; never uses the default profile'}

    @staticmethod
    def available():
        return find_browser() is not None

    find_executable=staticmethod(find_browser)

    def effects_confirmed(self):
        """Host-owned facts only; model text cannot clear this ledger."""
        return not self._uncertain and not any(effect['state'] in {'pending','unknown'} and effect.get('action_token')
            for tab in self._tabs.values() for effect in tab.get('network_effects',{}).values())

    @property
    def pending_effect(self):
        return not self.effects_confirmed()

    async def _ensure_started(self):
        if self._closed: raise BrowserError('BROWSER_SESSION_CLOSED')
        if self._connection is not None: return
        async with self._start_lock:
            if self._closed: raise BrowserError('BROWSER_SESSION_CLOSED')
            if self._connection is not None: return
            if self.executable is None or not self.executable.is_file(): raise BrowserError('BROWSER_NOT_INSTALLED')
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise BrowserError('BROWSER_DEPENDENCY_UNAVAILABLE') from None
            self._starting_task=asyncio.current_task()
            port=await self.proxy.start()
            self._profile=tempfile.TemporaryDirectory(prefix='aura-isolated-browser-')
            profile=Path(self._profile.name).resolve()
            if not profile.is_relative_to(Path(tempfile.gettempdir()).resolve()):
                raise BrowserError('BROWSER_PROFILE_INVALID')
            args=[str(self.executable),f'--user-data-dir={profile}','--remote-debugging-port=0',
                  '--remote-debugging-address=127.0.0.1',f'--proxy-server=http://127.0.0.1:{port}',
                  '--proxy-bypass-list=<-loopback>','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
                  '--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                  '--disable-features=DnsOverHttps,WebTransport,MediaRouter','--disable-background-networking',
                  '--disable-component-update','--disable-sync','--disable-default-apps','--no-first-run',
                  '--no-default-browser-check','--disable-extensions','--disable-popup-blocking',
                  '--window-size=1280,900','about:blank']
            if self.headless: args.insert(1,'--headless=new')
            launch={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
            try:
                self._process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,**launch)
                deadline=time.monotonic()+self.timeout
                endpoint=None
                while time.monotonic()<deadline:
                    if self._process.returncode is not None: raise BrowserError('BROWSER_START_FAILED')
                    try:
                        lines=(profile/'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
                        if len(lines)>=2 and lines[0].isdigit() and lines[1].startswith('/devtools/browser/'):
                            endpoint=f'ws://127.0.0.1:{int(lines[0])}{lines[1]}'
                            break
                    except OSError: pass
                    await asyncio.sleep(0.05)
                if endpoint is None: raise BrowserError('BROWSER_START_TIMEOUT')
                options={'open_timeout':self.timeout,'max_size':8*1024*1024,'ping_interval':20}
                if 'proxy' in inspect.signature(connect).parameters: options['proxy']=None
                websocket=await connect(endpoint,**options)
                self._connection=_CDP(websocket,self._event,self.timeout)
                await self._connection.call('Browser.setDownloadBehavior',{'behavior':'deny'})
                await self._connection.call('Target.setDiscoverTargets',{'discover':True})
                await self._connection.call('Target.setAutoAttach',{'autoAttach':True,'waitForDebuggerOnStart':True,'flatten':True})
                targets=await self._connection.call('Target.getTargets')
                for target in targets.get('targetInfos',[]):
                    if target.get('type')=='page' and target['targetId'] not in self._tabs:
                        attached=await self._connection.call('Target.attachToTarget',{'targetId':target['targetId'],'flatten':True})
                        await self._attach(target,attached['sessionId'])
                if not self._tabs:
                    await self._connection.call('Target.createTarget',{'url':'about:blank'})
                while time.monotonic()<deadline and not self._tabs: await asyncio.sleep(0.02)
                if not self._tabs: raise BrowserError('BROWSER_NO_PAGE')
                self._active=self._active or next(iter(self._tabs))
                await self._tab()
            except (Exception,asyncio.CancelledError):
                self._starting_task=None
                # An external close has cancelled and is joining this startup;
                # recursively waiting on its close lock would deadlock.
                if not self._closed: await self.close()
                raise
            self._starting_task=None

    async def _attach(self,target,session):
        if session in self._sessions: return
        existing=self._tabs.get(target.get('targetId'))
        if existing is not None and existing['session']!=session:
            with suppress(BrowserError):
                await self._connection.call('Target.detachFromTarget',{'sessionId':session})
            return
        tab=None
        if target.get('type')=='page':
            tab=self._tabs.setdefault(target['targetId'],{'id':target['targetId'],'session':session,'url':target.get('url','about:blank'),
                'title':target.get('title',''),'generation':0,'world':None,'ready':asyncio.Event(),'error':None,
                'network_effects':{},'action_token':None,'last_action_token':None})
            self._sessions[session]=tab['id']
        try:
            await self._connection.call('Runtime.enable',session=session)
            await self._connection.call('Network.enable',session=session)
            await self._connection.call('Fetch.enable',{'patterns':[{'urlPattern':'*','requestStage':'Request'}],'handleAuthRequests':True},session=session)
            with suppress(BrowserError):
                await self._connection.call('Target.setAutoAttach',{'autoAttach':True,'waitForDebuggerOnStart':True,'flatten':True},session=session)
            if tab is not None:
                await self._connection.call('Page.enable',session=session)
            await self._connection.call('Runtime.runIfWaitingForDebugger',session=session)
        except BrowserError:
            if tab is not None: tab['error']='BROWSER_PAGE_SETUP_FAILED'
            with suppress(BrowserError): await self._connection.call('Runtime.runIfWaitingForDebugger',session=session)
        finally:
            if tab is not None: tab['ready'].set()

    async def _event(self,message):
        if self._closed or self._connection is None: return
        method,params,session=message['method'],message.get('params',{}),message.get('sessionId')
        if method=='Target.attachedToTarget':
            await self._attach(params['targetInfo'],params['sessionId'])
        elif method=='Target.targetDestroyed':
            self._invalidate(params['targetId'])
            self._tabs.pop(params['targetId'],None)
        elif method=='Target.targetInfoChanged':
            info=params['targetInfo'];tab=self._tabs.get(info['targetId'])
            if tab is not None: tab.update(url=info.get('url',tab['url']),title=info.get('title',tab['title']))
        elif method in {'Page.frameNavigated','Page.navigatedWithinDocument','Runtime.executionContextsCleared'}:
            tab=self._tabs.get(self._sessions.get(session))
            if tab is not None and (method!='Page.frameNavigated' or not params.get('frame',{}).get('parentId')):
                self._invalidate(tab['id']);tab['generation']+=1;tab['world']=None
                if params.get('frame',{}).get('url'): tab['url']=params['frame']['url']
                if params.get('url'): tab['url']=params['url']
        elif method=='Fetch.authRequired':
            await self._connection.call('Fetch.continueWithAuth',{'requestId':params['requestId'],'authChallengeResponse':{'response':'CancelAuth'}},session=session)
        elif method=='Network.requestWillBeSent':
            tab=self._tabs.get(self._sessions.get(session))
            request=params.get('request',{})
            if tab is not None and request.get('method') in {'POST','PUT','PATCH','DELETE'}:
                effects=tab['network_effects']
                effects[params['requestId']]={'request_id':params['requestId'],'method':request['method'],
                    'state':'pending','action_token':tab.get('action_token') or tab.get('last_action_token')}
                while len(effects)>100: effects.pop(next(iter(effects)))
        elif method in {'Network.responseReceived','Network.loadingFinished','Network.loadingFailed'}:
            tab=self._tabs.get(self._sessions.get(session))
            effect=tab['network_effects'].get(params.get('requestId')) if tab is not None else None
            if effect is not None:
                if method=='Network.responseReceived': effect['http_status']=params.get('response',{}).get('status')
                elif method=='Network.loadingFailed': effect['state']='unknown'
                else: effect['state']='acknowledged' if 200<=effect.get('http_status',0)<400 else 'unknown'
                token=effect.get('action_token')
                if token in self._pending_network and all(item['state']=='acknowledged'
                    for item in tab['network_effects'].values() if item.get('action_token')==token):
                    self._uncertain.discard(self._pending_network.pop(token))
        elif method=='Fetch.requestPaused':
            try:
                await self.policy.validate(params['request']['url'])
                if params.get('resourceType') in {'Image','Media','Font'}:
                    raise BrowserNetworkError('BROWSER_UNNEEDED_RESOURCE')
            except BrowserNetworkError as exc:
                self._blocked.append({'reason':str(exc),'resource_type':params.get('resourceType')})
                del self._blocked[:-20]
                await self._connection.call('Fetch.failRequest',{'requestId':params['requestId'],'errorReason':'BlockedByClient'},session=session)
            else:
                await self._connection.call('Fetch.continueRequest',{'requestId':params['requestId']},session=session)
        elif method=='Page.javascriptDialogOpening':
            await self._connection.call('Page.handleJavaScriptDialog',{'accept':False},session=session)

    def _invalidate(self,identifier):
        self._references={ref:value for ref,value in self._references.items() if value['tab']!=identifier}

    def _cache_result(self,key,value):
        self._results[key]=value
        # Evicted known calls still cannot dispatch: their old refs were
        # invalidated after the action. Unknown effects also have a ledger.
        while len(self._results)>64: self._results.pop(next(iter(self._results)))

    async def _tab(self):
        tab=self._tabs.get(self._active)
        if tab is None: raise BrowserError('BROWSER_TAB_UNAVAILABLE')
        try: await asyncio.wait_for(tab['ready'].wait(),timeout=self.timeout)
        except asyncio.TimeoutError: raise BrowserError('BROWSER_PAGE_SETUP_TIMEOUT') from None
        if tab['error']: raise BrowserError(tab['error'])
        return tab

    async def _evaluate(self,tab,mode,args):
        if tab['world'] is None:
            tree=await self._connection.call('Page.getFrameTree',session=tab['session'])
            frame=tree['frameTree']['frame']
            tab['url']=frame.get('url',tab['url'])
            world=await self._connection.call('Page.createIsolatedWorld',{'frameId':frame['id'],'worldName':self._dom_key},session=tab['session'])
            tab['world']=world['executionContextId']
        try:
            result=await self._connection.call('Runtime.callFunctionOn',{'functionDeclaration':_DOM.read_text(encoding='utf-8'),
                'executionContextId':tab['world'],'arguments':[{'value':{'mode':mode,'args':args,'key':self._dom_key}}],
                'returnByValue':True,'awaitPromise':True,'userGesture':mode=='act'},session=tab['session'])
        except BrowserError as exc:
            if mode=='act': raise BrowserError(exc.code,uncertain=True) from None
            raise
        if result.get('exceptionDetails'): raise BrowserError('BROWSER_DOM_OPERATION_FAILED',uncertain=mode=='act')
        value=result.get('result',{}).get('value')
        if not isinstance(value,dict): raise BrowserError('BROWSER_DOM_RESULT_INVALID',uncertain=mode=='act')
        if value.get('error'): raise BrowserError(value['error'])
        return value

    async def _observe(self,query='',offset=0,limit=50,text_offset=0,text_limit=12000):
        if (not isinstance(query,str) or len(query)>512 or any(type(value) is not int or value<0 for value in (offset,text_offset))
                or type(limit) is not int or not 1<=limit<=100 or type(text_limit) is not int or not 1<=text_limit<=24000):
            raise BrowserError('BROWSER_INVALID_OBSERVATION')
        tab=await self._tab()
        if tab['url']!='about:blank': await self.policy.validate(tab['url'])
        args={'query':query,'offset':offset,'limit':limit,'text_offset':text_offset,'text_limit':text_limit}
        for attempt in range(2):
            try:
                value=await self._evaluate(tab,'observe',args)
                break
            except BrowserError as exc:
                if exc.code not in {'BROWSER_STALE_DOCUMENT','BROWSER_PROTOCOL_COMMAND_FAILED'} or attempt: raise
                tab['world']=None
        if value['url']!='about:blank': await self.policy.validate(value['url'])
        tab.update(url=value['url'],title=value['title'])
        for element in value['elements']:
            ref='br_'+secrets.token_urlsafe(18)
            self._references[ref]={'tab':tab['id'],'generation':tab['generation'],'node':element.pop('node'),
                'fingerprint':element.pop('fingerprint'),'created':time.monotonic(), 'href':element.get('href')}
            element['ref']=ref
        while len(self._references)>2000: self._references.pop(next(iter(self._references)))
        effects=[{**{key:value for key,value in effect.items() if key!='action_token'},
                  'associated_action':bool(effect.get('action_token'))} for effect in tab.get('network_effects',{}).values()]
        value.update(tab_id=tab['id'],untrusted_source=True,session_scope='owned anonymous isolated profile',blocked_requests=list(self._blocked),
            browser_environment={'visible':not self.headless,'uses_user_profile':False,'closes_when_task_ends':True},
            network_effects=effects,pending_effect=self.pending_effect,effect_state='uncertain' if self.pending_effect else 'observed')
        return value

    async def navigate(self,url,*,new_tab=False):
        if type(new_tab) is not bool: raise BrowserError('BROWSER_INVALID_NAVIGATION')
        await self.policy.validate(url)
        async with self._operation_lock:
            await self._ensure_started()
            if new_tab:
                result=await self._connection.call('Target.createTarget',{'url':'about:blank'})
                self._active=result['targetId']
                deadline=time.monotonic()+self.timeout
                while self._active not in self._tabs and time.monotonic()<deadline: await asyncio.sleep(0.02)
            tab=await self._tab()
            self._invalidate(tab['id'])
            result=await self._connection.call('Page.navigate',{'url':url},session=tab['session'])
            if result.get('errorText'): raise BrowserError('BROWSER_NAVIGATION_BLOCKED')
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                try:
                    value=await self._observe(limit=30)
                    if value['url']!='about:blank' and value.get('ready_state') in {'interactive','complete'}:
                        return json.dumps(value,ensure_ascii=False)
                except BrowserError as exc:
                    if exc.code not in {'BROWSER_PROTOCOL_COMMAND_FAILED','BROWSER_STALE_DOCUMENT','BROWSER_DOM_OPERATION_FAILED'}: raise
                    tab['world']=None
                await asyncio.sleep(0.05)
            raise BrowserError('BROWSER_NAVIGATION_TIMEOUT',uncertain=True)

    async def observe(self,query='',offset=0,limit=50,*,text_offset=0,text_limit=12000):
        async with self._operation_lock:
            await self._ensure_started()
            return json.dumps(await self._observe(query,offset,limit,text_offset,text_limit),ensure_ascii=False)

    async def act(self,ref,action,value=''):
        if not isinstance(ref,str) or action not in _ACTIONS or not isinstance(value,str) or len(value)>16384:
            raise BrowserError('BROWSER_INVALID_ACTION')
        if action=='press' and value not in _KEYS: raise BrowserError('BROWSER_UNSUPPORTED_KEY')
        async with self._operation_lock:
            await self._ensure_started()
            cache_key=(ref,action,hashlib.sha256(value.encode()).hexdigest())
            if cache_key in self._results:
                cached=json.loads(self._results[cache_key])
                cached.update(reused_result=True,new_input_dispatched=False)
                return json.dumps(cached,ensure_ascii=False)
            current=self._references.get(ref)
            tab=await self._tab()
            if current is None or current['tab']!=tab['id'] or current['generation']!=tab['generation'] or time.monotonic()-current['created']>60:
                raise BrowserError('BROWSER_STALE_REFERENCE')
            semantic=(tab['id'],current['generation'],current['node'])
            if semantic in self._uncertain: raise BrowserError('BROWSER_EFFECT_UNCERTAIN',uncertain=True)
            if action=='click' and current.get('href'): await self.policy.validate(current['href'])
            args={**current,'action':action,'value':value}
            # Preflight happens before dispatch; a stale or private ref is not
            # an unknown side effect and may be observed again safely.
            await self._evaluate(tab,'inspect',args)
            self._uncertain.add(semantic)
            token=secrets.token_hex(12)
            tab['action_token']=token
            tab['last_action_token']=token
            try:
                result=await self._evaluate(tab,'act',args)
                if action=='press':
                    code=_KEYS[value]
                    for kind in ('keyDown','keyUp'):
                        await self._connection.call('Input.dispatchKeyEvent',{'type':kind,'key':' ' if value=='Space' else value,
                            'windowsVirtualKeyCode':code,'nativeVirtualKeyCode':code},session=tab['session'])
                self._uncertain.discard(semantic)
                self._invalidate(tab['id'])
                await asyncio.sleep(0.10)
                deadline=time.monotonic()+min(self.timeout,10)
                while time.monotonic()<deadline and any(effect['state']=='pending' and effect.get('action_token')==token
                    for effect in tab.get('network_effects',{}).values()):
                    await asyncio.sleep(0.05)
                relevant=[effect for effect in tab.get('network_effects',{}).values() if effect.get('action_token')==token]
                unresolved=any(effect['state'] in {'pending','unknown'} for effect in relevant)
                if unresolved:
                    self._uncertain.add(semantic)
                    self._pending_network[token]=semantic
                post=None
                for _ in range(3):
                    try: post=await self._observe(limit=30);break
                    except BrowserError:
                        tab['world']=None
                        await asyncio.sleep(0.05)
                output={'status':'needs_user' if unresolved else 'completed' if result.get('property_verified') else 'observed',
                    'action':action,'control_result':result,'post_observation':post,'untrusted_source':True,
                    'effect_state':'uncertain' if unresolved else 'observed','pending_effect':unresolved,
                    'network_effects':[{key:value for key,value in effect.items() if key!='action_token'} for effect in relevant],
                    'goal_verified':False,'scope':'DOM action and fresh observation; overall user goal still requires verification.'}
                encoded=json.dumps(output,ensure_ascii=False)
                self._cache_result(cache_key,encoded)
                return encoded
            except asyncio.CancelledError:
                raise
            except BrowserError as exc:
                if not exc.uncertain:
                    # Fixed DOM validation errors occur before its mutation.
                    self._uncertain.discard(semantic)
                self._cache_result(cache_key,json.dumps({'status':'needs_user','code':exc.code,'outcome_unknown':exc.uncertain,
                    'effect_state':exc.effect_state,'pending_effect':exc.pending_effect,
                    'scope':'Do not repeat a dispatched action with unknown outcome.'}))
                raise
            finally:
                tab['action_token']=None

    async def tabs(self):
        async with self._operation_lock:
            await self._ensure_started()
            result=await self._connection.call('Target.getTargets')
            tabs=[]
            for target in result.get('targetInfos',[]):
                if target.get('type')=='page' and target['targetId'] in self._tabs:
                    tabs.append({'tab_id':target['targetId'],'title':target.get('title',''),'url':target.get('url',''),
                                 'active':target['targetId']==self._active})
            return json.dumps({'tabs':tabs,'untrusted_source':True,'session_scope':'owned isolated profile only'},ensure_ascii=False)

    async def switch(self,tab_id):
        async with self._operation_lock:
            await self._ensure_started()
            if not isinstance(tab_id,str) or tab_id not in self._tabs: raise BrowserError('BROWSER_TAB_UNAVAILABLE')
            await self._connection.call('Target.activateTarget',{'targetId':tab_id})
            self._active=tab_id
            return json.dumps(await self._observe(limit=30),ensure_ascii=False)

    async def close(self):
        async with self._close_lock:
            if self._close_complete: return
            self._closed=True
            starting=self._starting_task
            if starting is not None and starting is not asyncio.current_task() and not starting.done():
                starting.cancel()
                await asyncio.gather(starting,return_exceptions=True)
            failed=False
            try:
                if self._connection is not None:
                    with suppress(Exception): await self._connection.call('Browser.close')
                    await self._connection.close()
                    self._connection=None
            except Exception:
                failed=True
            try:
                if self._process is not None and self._process.returncode is None:
                    try: await asyncio.wait_for(self._process.wait(),timeout=3)
                    except asyncio.TimeoutError:
                        if os.name=='nt':
                            process=await asyncio.create_subprocess_exec('taskkill','/PID',str(self._process.pid),'/T','/F',
                                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                            with suppress(asyncio.TimeoutError): await asyncio.wait_for(process.wait(),timeout=3)
                        else:
                            import signal
                            with suppress(ProcessLookupError): os.killpg(self._process.pid,signal.SIGKILL)
                        with suppress(asyncio.TimeoutError): await asyncio.wait_for(self._process.wait(),timeout=3)
                    if self._process.returncode is None: failed=True
            except Exception:
                failed=True
            # A failed CDP/process close must still release the owned proxy.
            # Retain unresolved objects so an explicit second close can retry.
            try: await self.proxy.close()
            except Exception: failed=True
            self._references.clear();self._tabs.clear();self._sessions.clear()
            if self._profile is not None:
                if self._process is not None and self._process.returncode is None:
                    failed=True
                else:
                    try:
                        await self._cleanup_profile()
                    except OSError:
                        failed=True
                    else:
                        self._profile=None
            if failed: raise BrowserError('BROWSER_CLEANUP_UNCONFIRMED') from None
            self._close_complete=True

    async def _cleanup_profile(self):
        """Retry transient sharing of this owned profile after process exit.

        This never replays a browser action. Persistent failures retain the
        profile and keep the existing unconfirmed-cleanup fence in place.
        """
        profile = self._profile
        delays = (.05, .1, .15, .2, .25)
        for attempt in range(len(delays) + 1):
            owned = False
            if isinstance(profile, tempfile.TemporaryDirectory):
                path = Path(profile.name)
                owned = (path.name.startswith('aura-isolated-browser-')
                    and path.resolve().parent == Path(tempfile.gettempdir()).resolve())
                try:
                    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
                except FileNotFoundError:
                    attributes = 0  # An already-removed owned path is harmless.
                owned = owned and not path.is_symlink() and not attributes & 0x400
                if not owned:
                    raise OSError(errno.EPERM, 'BROWSER_PROFILE_SCOPE_UNCONFIRMED')
            try:
                profile.cleanup()
                return
            except OSError as error:
                transient = (getattr(error, 'winerror', None) in {5, 32, 33, 145}
                    or error.errno in {errno.EACCES, errno.EPERM, errno.EBUSY, errno.ENOTEMPTY})
                # Real temporary directories are checked before every attempt.
                # Foreign implementations retain their existing single-call
                # behavior, including test doubles; they are never retried.
                if not transient or not owned or attempt == len(delays):
                    raise
                await asyncio.sleep(delays[attempt])
