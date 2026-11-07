"""Anonymous browser policy, DOM state, tabs and private-network boundaries."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_network import BrowserNetworkError,PublicBrowserPolicy,PinnedPublicProxy
from browser_session import BrowserError,BrowserSession,find_browser


class BrowserPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_policy_rejects_private_schemes_credentials_and_unapproved_ports(self):
        policy=PublicBrowserPolicy()
        for url in ('file:///C:/Windows/win.ini','javascript:alert(1)','data:text/html,private',
                    'http://127.0.0.1/','http://localhost/','http://169.254.169.254/latest/meta-data',
                    'http://10.1.2.3/','https://user:password@example.invalid/', 'http://127.0.0.1:12345/'):
            with self.subTest(url=url),self.assertRaises(BrowserNetworkError):
                await policy.validate(url)

    async def test_only_host_constructor_can_grant_exact_local_fixture_origin(self):
        policy=PublicBrowserPolicy(allowed_test_hosts={('127.0.0.1',12345)})
        target=await policy.validate('http://127.0.0.1:12345/fixture')
        self.assertEqual(target[3],'127.0.0.1')
        with self.assertRaises(BrowserNetworkError): await policy.validate('http://127.0.0.1:12346/fixture')

    async def test_private_navigation_rejected_before_browser_or_proxy_starts(self):
        browser=BrowserSession()
        with self.assertRaises(BrowserNetworkError): await browser.navigate('http://127.0.0.1/')
        self.assertIsNone(browser._process)
        self.assertIsNone(browser.proxy.port)
        await browser.close()

    async def test_proxy_uses_validated_ip_instead_of_resolving_origin_again(self):
        policy=PublicBrowserPolicy()
        proxy=PinnedPublicProxy(policy)
        forwarded=[]
        class Writer:
            def __init__(self): self.data=b'';self.closed=False
            def write(self,data): self.data+=data
            async def drain(self): pass
            def close(self): self.closed=True
            async def wait_closed(self): pass
            def is_closing(self): return self.closed
        incoming=asyncio.StreamReader()
        incoming.feed_data(b'GET http://public.example/path HTTP/1.1\r\nHost: malicious.invalid\r\n\r\n');incoming.feed_eof()
        outgoing=Writer()
        async def target(url): return (type('Parsed',(),{'scheme':'http','path':'/path','query':''})(),'public.example',80,'93.184.216.34')
        async def connect(host,port):
            forwarded.append((host,port))
            reader=asyncio.StreamReader();reader.feed_data(b'HTTP/1.0 200 OK\r\n\r\nfixture');reader.feed_eof()
            return reader,Writer()
        with patch.object(policy,'validate',target),patch('browser_network.asyncio.open_connection',connect):
            await proxy._accept(incoming,outgoing)
        self.assertEqual(forwarded,[('93.184.216.34',80)])
        self.assertIn(b'200 OK',outgoing.data)

    async def test_uncertain_effect_cannot_be_repeated_with_a_new_reference(self):
        browser=BrowserSession()
        tab={'id':'owned','session':'session','generation':1,'ready':asyncio.Event(),'world':1,'url':'https://public.example/'}
        tab['ready'].set()
        browser._tabs['owned']=tab;browser._active='owned'
        async def started(): pass
        async def selected(): return tab
        async def evaluate(tab,mode,args):
            if mode=='act': raise BrowserError('BROWSER_COMMAND_TIMEOUT',uncertain=True)
            return {'state':{}}
        import time
        reference={'tab':'owned','generation':1,'node':'same-node','fingerprint':'fixture','created':time.monotonic(),'href':None}
        browser._references['br_old']=dict(reference)
        browser._references['br_new']=dict(reference)
        with patch.object(browser,'_ensure_started',started),patch.object(browser,'_tab',selected),patch.object(browser,'_evaluate',evaluate):
            with self.assertRaises(BrowserError): await browser.act('br_old','click')
            with self.assertRaisesRegex(BrowserError,'BROWSER_EFFECT_UNCERTAIN'):
                await browser.act('br_new','click')
        await browser.close()

    async def test_network_acknowledgment_clears_only_host_tracked_pending_effect(self):
        browser=BrowserSession();semantic=('owned',1,'node')
        browser._connection=object()
        browser._sessions['session']='owned'
        browser._tabs['owned']={'id':'owned','network_effects':{},'action_token':'owned-action'}
        browser._pending_network['owned-action']=semantic;browser._uncertain.add(semantic)
        await browser._event({'method':'Network.requestWillBeSent','sessionId':'session',
            'params':{'requestId':'request','request':{'method':'POST'}}})
        self.assertFalse(browser.effects_confirmed())
        await browser._event({'method':'Network.responseReceived','sessionId':'session',
            'params':{'requestId':'request','response':{'status':204}}})
        self.assertFalse(browser.effects_confirmed())
        await browser._event({'method':'Network.loadingFinished','sessionId':'session','params':{'requestId':'request'}})
        self.assertTrue(browser.effects_confirmed())
        await browser._event({'method':'Network.requestWillBeSent','sessionId':'session',
            'params':{'requestId':'second','request':{'method':'POST'}}})
        await browser._event({'method':'Network.loadingFailed','sessionId':'session','params':{'requestId':'second'}})
        self.assertFalse(browser.effects_confirmed())
        browser._connection=None
        await browser.close()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        self.server.requests.append(self.path)
        if self.path=='/second':
            body='<html><meta charset="utf-8"><title>第二页</title><body><p id="second">第二页的真实内容</p><button>第二页按钮</button></body></html>'
        else:
            body='''<html><meta charset="utf-8"><title>AURA 隔离验收</title><body>
<label for="name">昵称</label><input id="name"><label for="note">备注</label><textarea id="note"></textarea>
<input type="password" id="password" value="PASSWORD-MUST-NOT-EXPORT">
<input type="checkbox" id="checked"><label for="checked">启用测试</label>
<button id="save" onclick="document.querySelector('#status').innerText='已保存 '+document.querySelector('#name').value;fetch('/save',{method:'POST',body:JSON.stringify({name:document.querySelector('#name').value})});">保存</button>
<button id="unknown" onclick="fetch('/unknown',{method:'POST',body:'fixture'}).catch(()=>{});">提交后返回错误</button>
<p id="status" role="status">尚未保存</p><a href="/second" target="_blank">打开第二页</a>
<a href="http://169.254.169.254/latest/meta-data">应拦截的私网链接</a>
<script>fetch('http://127.0.0.1:RESTRICTED_PORT/forbidden').catch(()=>{});</script>
</body></html>'''.replace('RESTRICTED_PORT',str(self.server.restricted_port))
        raw=body.encode('utf-8');self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        if self.path=='/unknown':
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.server.unknown_saves+=1
            self.send_response(500);self.send_header('Content-Length','0');self.end_headers()
            return
        self.server.saves.append(json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')))))
        self.send_response(204);self.end_headers()


@unittest.skipUnless(find_browser() and importlib.util.find_spec('websockets'),'isolated Edge/Chrome and websockets required')
class ActualIsolatedBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.restricted=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
        cls.restricted.requests=[];cls.restricted.saves=[];cls.restricted.unknown_saves=0;cls.restricted.restricted_port=cls.restricted.server_port
        cls.restricted_thread=threading.Thread(target=cls.restricted.serve_forever,daemon=True);cls.restricted_thread.start()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
        cls.server.requests=[];cls.server.saves=[];cls.server.unknown_saves=0;cls.server.restricted_port=cls.restricted.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        for server,thread in ((cls.server,cls.thread),(cls.restricted,cls.restricted_thread)):
            server.shutdown();server.server_close();thread.join(timeout=2)

    async def asyncSetUp(self):
        self.browser=BrowserSession(allowed_test_hosts={('127.0.0.1',self.server.server_port)})
        self.url=f'http://127.0.0.1:{self.server.server_port}/'
        self.start_saves=len(self.server.saves)

    async def asyncTearDown(self):
        profile=Path(self.browser._profile.name) if self.browser._profile is not None else None
        await self.browser.close()
        if self.browser._process is not None: self.assertIsNotNone(self.browser._process.returncode)
        if self.browser.proxy.server is not None: self.assertFalse(self.browser.proxy.server.is_serving())
        if profile is not None: self.assertFalse(profile.exists())

    async def test_real_navigate_fill_click_postcondition_tabs_and_stale_references(self):
        first=json.loads(await self.browser.navigate(self.url))
        self.assertEqual(first['title'],'AURA 隔离验收')
        self.assertEqual(first['browser_environment'],
                         {'visible':False,'uses_user_profile':False,'closes_when_task_ends':True})
        self.assertNotIn('PASSWORD-MUST-NOT-EXPORT',json.dumps(first))
        name=next(item for item in first['elements'] if item['label']=='昵称')
        filled=json.loads(await self.browser.act(name['ref'],'fill','鲸鱼娘 😀'))
        self.assertEqual(filled['status'],'completed')
        self.assertEqual(filled['control_result']['after']['value'],'鲸鱼娘 😀')
        save=next(item for item in filled['post_observation']['elements'] if item['label']=='保存')
        result=json.loads(await self.browser.act(save['ref'],'click'))
        self.assertIn('已保存 鲸鱼娘 😀',result['post_observation']['text'])
        self.assertEqual(self.server.saves[-1]['name'],'鲸鱼娘 😀')
        cached=json.loads(await self.browser.act(save['ref'],'click'))
        self.assertTrue(cached['reused_result'])
        self.assertFalse(cached['new_input_dispatched'])
        self.assertEqual(len(self.server.saves)-self.start_saves,1)
        next_page=json.loads(await self.browser.navigate(self.url+'second',new_tab=True))
        self.assertEqual(next_page['title'],'第二页')
        tabs=json.loads(await self.browser.tabs())['tabs'];self.assertEqual(len(tabs),2)
        previous=next(tab for tab in tabs if tab['url']==self.url)
        back=json.loads(await self.browser.switch(previous['tab_id']))
        self.assertIn('已保存 鲸鱼娘 😀',back['text'])
        with self.assertRaisesRegex(BrowserError,'STALE_REFERENCE'):
            await self.browser.act(name['ref'],'fill','stale should fail')
        self.assertFalse(self.restricted.requests)
        self.assertTrue(any(item['reason']=='BROWSER_PUBLIC_TARGET_REQUIRED' for item in back['blocked_requests']))

    async def test_navigation_invalidates_refs_and_checked_state_is_idempotent(self):
        first=json.loads(await self.browser.navigate(self.url))
        checkbox=next(item for item in first['elements'] if item.get('type')=='checkbox')
        checked=json.loads(await self.browser.act(checkbox['ref'],'check'))
        self.assertTrue(checked['control_result']['property_verified'])
        checkbox2=next(item for item in checked['post_observation']['elements'] if item.get('type')=='checkbox')
        again=json.loads(await self.browser.act(checkbox2['ref'],'check'))
        self.assertFalse(again['control_result']['changed'])
        self.assertTrue(again['control_result']['property_verified'])
        before=next(item for item in again['post_observation']['elements'] if item['label']=='昵称')
        await self.browser.navigate(self.url+'second')
        with self.assertRaisesRegex(BrowserError,'STALE_REFERENCE'):
            await self.browser.act(before['ref'],'fill','must not type')

    async def test_private_link_is_rejected_before_click_dispatch(self):
        first=json.loads(await self.browser.navigate(self.url))
        link=next(item for item in first['elements'] if item['label']=='应拦截的私网链接')
        with self.assertRaises(BrowserNetworkError): await self.browser.act(link['ref'],'click')
        current=json.loads(await self.browser.observe())
        self.assertEqual(current['url'],self.url)

    async def test_post_error_after_server_effect_remains_uncertain_and_is_never_reclicked(self):
        before=self.server.unknown_saves
        first=json.loads(await self.browser.navigate(self.url))
        button=next(item for item in first['elements'] if item['label']=='提交后返回错误')
        result=json.loads(await self.browser.act(button['ref'],'click'))
        self.assertEqual(self.server.unknown_saves,before+1)
        self.assertEqual(result['status'],'needs_user')
        self.assertEqual(result['effect_state'],'uncertain')
        self.assertTrue(result['pending_effect'])
        self.assertFalse(self.browser.effects_confirmed())
        again=json.loads(await self.browser.act(button['ref'],'click'))
        self.assertTrue(again['pending_effect'])
        self.assertTrue(again['reused_result'])
        self.assertFalse(again['new_input_dispatched'])
        self.assertEqual(self.server.unknown_saves,before+1)
        refreshed=json.loads(await self.browser.observe())
        new_button=next(item for item in refreshed['elements'] if item['label']=='提交后返回错误')
        with self.assertRaisesRegex(BrowserError,'EFFECT_UNCERTAIN'):
            await self.browser.act(new_button['ref'],'click')


if __name__=='__main__': unittest.main()
