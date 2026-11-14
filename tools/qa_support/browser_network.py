"""Pinned public-network boundary for an owned, anonymous browser process."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import socket
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from .public_web import PublicWebError, public_target
    from .http_response import HttpResponseState, relay_final_http_response
except ImportError:
    from public_web import PublicWebError, public_target
    from http_response import HttpResponseState, relay_final_http_response


class BrowserNetworkError(ValueError):
    def __init__(self,code):
        self.code=code
        self.effect_state='not_dispatched'
        self.pending_effect=False
        super().__init__(code)


class PublicBrowserPolicy:
    """Only the trusted test harness can opt specific loopback origins in."""
    def __init__(self, *, allowed_test_hosts=()):
        self.allowed_test_hosts = frozenset(allowed_test_hosts)
        self._cache = {}

    def target(self, url):
        if not isinstance(url, str) or len(url)>4096 or any(ord(c)<32 for c in url):
            raise BrowserNetworkError('BROWSER_INVALID_URL')
        try:
            parsed=urlsplit(url)
            host=(parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
            port=parsed.port or (443 if parsed.scheme=='https' else 80)
        except (ValueError, UnicodeError):
            raise BrowserNetworkError('BROWSER_INVALID_URL') from None
        if parsed.scheme not in {'http','https'} or not host or parsed.username is not None or parsed.password is not None:
            raise BrowserNetworkError('BROWSER_INVALID_URL')
        # Testing is a constructor-only capability, not a tool argument or env.
        if host in {'127.0.0.1','localhost','::1'} and (host,port) in self.allowed_test_hosts:
            return parsed,host,port,'127.0.0.1' if host=='localhost' else host
        key=(parsed.scheme,host,port)
        cached=self._cache.get(key)
        if cached and time.monotonic()-cached[0]<30:
            return parsed,host,port,cached[1]
        try:
            result=public_target(url)
        except (PublicWebError, OSError, ValueError):
            raise BrowserNetworkError('BROWSER_PUBLIC_TARGET_REQUIRED') from None
        if len(self._cache)>256:
            self._cache.clear()
        self._cache[key]=(time.monotonic(),result[3])
        return result

    async def validate(self, url):
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.target,url),timeout=10)
        except asyncio.TimeoutError:
            raise BrowserNetworkError('BROWSER_DNS_TIMEOUT') from None


class _OwnedProxyListener:
    """The listener and accept continuation share one explicit owner."""
    def __init__(self, owner, listener):
        self.owner, self.listener = owner, listener
    @property
    def sockets(self): return () if self.listener.fileno() < 0 else (self.listener,)
    def is_serving(self):
        task = self.owner._accept_task
        return not self.owner.closed and bool(self.sockets) and task is not None and not task.done()
    def close(self):
        future = self.owner._accept_future
        if future is not None and not future.done(): future.cancel()
        if self.owner._reader_fd is not None:
            asyncio.get_running_loop().remove_reader(self.owner._reader_fd)
            self.owner._reader_fd = None
        self.listener.close()
    async def wait_closed(self):
        task = self.owner._accept_task
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)


class PinnedPublicProxy:
    """HTTP/CONNECT proxy pins each upstream socket to the validated public IP.

    CDP URL interception alone cannot prevent DNS rebinding because Chromium
    resolves the host separately. The browser is forced through this proxy;
    no cookies, TLS interception, credential headers, or user profiles are read.
    """
    def __init__(self, policy):
        self.policy=policy
        self.server=None
        self.port=None
        self.tasks=set()
        self.writers=set()
        self.closed=False
        self.slots=asyncio.Semaphore(24)
        self._accept_task = None
        self._accept_future = None
        self._reader_fd = None
        self._protocols = set()
        self._relay_tasks = set()
        self._close_lock = asyncio.Lock()
        self._close_complete = False
        self._handler_failed = False

    async def start(self):
        if self.closed: raise BrowserNetworkError('BROWSER_PROXY_CLOSED')
        if self.server is not None: return self.port
        loop=asyncio.get_running_loop()
        if os.name == 'nt' and getattr(loop, '_proactor', None) is None:
            raise BrowserNetworkError('BROWSER_PROXY_EVENT_LOOP_UNSUPPORTED')
        owner = self
        class ResetSafeReaderProtocol(asyncio.StreamReaderProtocol):
            # CPython 3.12's Windows Proactor transport can raise WinError10054
            # while shutting down a reset socket, before detaching its server.
            # Close only this owned, already-lost socket in the protocol hook;
            # the transport then sees fileno=-1 and completes normal teardown.
            def connection_made(self,transport):
                self._aura_transport=transport
                super().connection_made(transport)
            def connection_lost(self,exc):
                if os.name=='nt' and self._aura_transport is not None:
                    wrapper=self._aura_transport.get_extra_info('socket')
                    raw=getattr(wrapper,'_sock',None)
                    if raw is not None:
                        with suppress(OSError): raw.shutdown(socket.SHUT_RDWR)
                        raw.close()
                try: super().connection_lost(exc)
                finally:
                    owner._protocols.discard(self)
                    if not self._aura_closed.done(): self._aura_closed.set_result(None)
        def connected(reader, writer):
            # Register synchronously in connection_made, before a newly-created
            # handler can lose a race to close()'s ownership snapshot.
            owner.writers.add(writer)
            task = asyncio.create_task(owner._accept(reader, writer))
            owner.tasks.add(task)
            def finished(task):
                owner.tasks.discard(task)
                owner.writers.discard(writer)
                if not task.cancelled() and task.exception() is not None:
                    owner._handler_failed = True
                    loop.call_exception_handler({'message': 'Jingban owned proxy handler failed',
                                                  'exception': task.exception(), 'task': task})
            task.add_done_callback(finished)
        def factory():
            protocol = ResetSafeReaderProtocol(asyncio.StreamReader(limit=65536),connected)
            protocol._aura_transport = None
            protocol._aura_closed = loop.create_future()
            owner._protocols.add(protocol)
            return protocol
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setblocking(False)
            listener.bind(('127.0.0.1', 0))
            listener.listen(64)
        except BaseException:
            listener.close()
            raise
        self.server = _OwnedProxyListener(self, listener)
        self.port=self.server.sockets[0].getsockname()[1]
        self._accept_task = asyncio.create_task(self._accept_loop(listener, factory))
        return self.port

    def _begin_accept(self, listener):
        loop = asyncio.get_running_loop()
        if os.name == 'nt':
            # Keep the raw IOCP future: cancelling a Task around sock_accept
            # can discard an already-completed (socket, address) result before
            # the coroutine takes ownership. No asyncio.Server owns transports.
            proactor = getattr(loop, '_proactor', None)
            if proactor is None: raise BrowserNetworkError('BROWSER_PROXY_EVENT_LOOP_UNSUPPORTED')
            return proactor.accept(listener)
        future = loop.create_future()
        descriptor = listener.fileno()
        self._reader_fd = descriptor
        def ready():
            if future.done(): return
            try:
                connection, address = listener.accept()
                connection.setblocking(False)
            except BlockingIOError: return
            except OSError as exc: future.set_exception(exc)
            else: future.set_result((connection, address))
            loop.remove_reader(descriptor)
            self._reader_fd = None
        loop.add_reader(descriptor, ready)
        return future

    async def _accept_loop(self, listener, factory):
        loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                future = self._begin_accept(listener)
                self._accept_future = future
                adopted = False
                try:
                    connection, _ = await asyncio.shield(future)
                    if self.closed: return
                    await loop.connect_accepted_socket(factory, connection)
                    adopted = True
                finally:
                    if not future.done(): future.cancel()
                    # Even a completed accept cancelled before this coroutine
                    # resumed still owns a socket; harvest it synchronously.
                    if not adopted and future.done() and not future.cancelled():
                        try: future.result()[0].close()
                        except OSError: pass
                    self._accept_future = None
                    await asyncio.gather(future, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except OSError:
            if not self.closed:
                self._handler_failed = True
                raise BrowserNetworkError('BROWSER_PROXY_ACCEPT_FAILED') from None
        except Exception:
            self._handler_failed = True
            raise

    async def _accept(self, reader, writer):
        if self.closed or len(self.tasks)>=64:
            writer.close()
            with suppress(Exception): await writer.wait_closed()
            return
        task=asyncio.current_task()
        self.tasks.add(task);self.writers.add(writer)
        remote=None
        response_state=HttpResponseState()
        try:
            async with self.slots:
                header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),timeout=10)
                if len(header)>32768:
                    raise BrowserNetworkError('BROWSER_PROXY_REQUEST_LIMIT')
                lines=header.decode('iso-8859-1').split('\r\n')
                parts=lines[0].split(' ')
                if len(parts)!=3 or parts[2] not in {'HTTP/1.0','HTTP/1.1'}:
                    raise BrowserNetworkError('BROWSER_PROXY_INVALID_REQUEST')
                method,target,_=parts
                if method=='CONNECT':
                    parsed,host,port,address=await self.policy.validate('https://'+target+'/')
                    remote_reader,remote=await asyncio.wait_for(asyncio.open_connection(address,port),timeout=10)
                    self.writers.add(remote)
                    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
                    await self._duplex(reader,writer,remote_reader,remote)
                    return
                if method not in {'GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'}:
                    raise BrowserNetworkError('BROWSER_PROXY_METHOD_UNSUPPORTED')
                parsed,host,port,address=await self.policy.validate(target)
                if parsed.scheme!='http':
                    raise BrowserNetworkError('BROWSER_PROXY_TUNNEL_REQUIRED')
                headers={}
                for line in lines[1:]:
                    if not line:
                        continue
                    if ':' not in line:
                        raise BrowserNetworkError('BROWSER_PROXY_INVALID_REQUEST')
                    name,value=line.split(':',1)
                    key=name.strip().lower()
                    if key in headers:
                        raise BrowserNetworkError('BROWSER_PROXY_DUPLICATE_HEADER')
                    headers[key]=(name,value.strip())
                if 'transfer-encoding' in headers:
                    raise BrowserNetworkError('BROWSER_PROXY_CHUNKED_REQUEST_UNSUPPORTED')
                length=int(headers.get('content-length',('', '0'))[1])
                if not 0<=length<=4*1024*1024:
                    raise BrowserNetworkError('BROWSER_PROXY_REQUEST_LIMIT')
                body=await asyncio.wait_for(reader.readexactly(length),timeout=15) if length else b''
                remote_reader,remote=await asyncio.wait_for(asyncio.open_connection(address,port),timeout=10)
                self.writers.add(remote)
                path=urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
                authority=('['+host+']' if ':' in host else host)+(f':{port}' if port!=80 else '')
                output=[f'{method} {path} HTTP/1.1',f'Host: {authority}','Connection: close']
                for key,(name,value) in headers.items():
                    if key not in {'host','connection','proxy-connection','proxy-authorization','proxy-authenticate'}:
                        output.append(f'{name}: {value}')
                remote.write(('\r\n'.join(output)+'\r\n\r\n').encode('iso-8859-1')+body)
                await remote.drain()
                await self._relay_http_response(remote_reader,writer,response_state)
        except (BrowserNetworkError,ValueError,OSError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):
            if not response_state.final_headers_forwarded and not writer.is_closing():
                with suppress(Exception):
                    denied = b'Jingban browser policy denied'
                    writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: '
                                 + str(len(denied)).encode('ascii') + b'\r\n\r\n' + denied)
                    await writer.drain()
        finally:
            for stream in (remote,writer):
                if stream is not None:
                    self.writers.discard(stream)
                    stream.close()
                    with suppress(Exception):
                        await asyncio.wait_for(stream.wait_closed(),timeout=1)
            self.tasks.discard(task)

    async def _relay_http_response(self, reader, writer, state):
        # Only the plain-HTTP branch calls this. CONNECT retains its original
        # encrypted duplex path; a 502 does not prove no remote effect occurred.
        return await relay_final_http_response(reader,writer,self._pipe,state=state,timeout=15)

    async def _pipe(self, reader, writer):
        total=0
        while not self.closed:
            data=await asyncio.wait_for(reader.read(65536),timeout=30)
            if not data:
                return
            total+=len(data)
            if total>64*1024*1024:
                raise BrowserNetworkError('BROWSER_PROXY_TRANSFER_LIMIT')
            writer.write(data);await writer.drain()

    async def _duplex(self, left_reader,left_writer,right_reader,right_writer):
        left=asyncio.create_task(self._pipe(left_reader,right_writer))
        right=asyncio.create_task(self._pipe(right_reader,left_writer))
        children = (left, right)
        self._relay_tasks.update(children)
        try:
            done,_=await asyncio.wait(children,return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
        finally:
            for task in children:
                if not task.done(): task.cancel()
            try:
                await asyncio.gather(*children,return_exceptions=True)
            finally:
                # A second cancellation can interrupt the join itself. Forget
                # only finished children; close() still owns any pending pipe.
                self._release_finished_relays(children)

    def _release_finished_relays(self, tasks):
        finished = {task for task in tasks if task in self._relay_tasks and task.done()}
        for task in finished:
            if not task.cancelled():
                error = task.exception()
                # Match the request handler's expected network/policy errors.
                # An owner cancelled during gather cannot report an unexpected
                # child failure itself, so preserve the existing failure flag.
                if error is not None and not isinstance(error, (BrowserNetworkError,
                        ValueError, OSError, asyncio.TimeoutError,
                        asyncio.IncompleteReadError, asyncio.LimitOverrunError)):
                    self._handler_failed = True
        self._relay_tasks.difference_update(finished)

    async def close(self):
        async with self._close_lock:
            if self._close_complete:
                if self._handler_failed: raise BrowserNetworkError('BROWSER_PROXY_HANDLER_FAILED')
                return
            self.closed=True
            if self.server is not None: self.server.close()
            if self._accept_task is not None:
                self._accept_task.cancel()
                await asyncio.gather(self._accept_task,return_exceptions=True)
            writers = list(self.writers)
            for writer in writers: writer.close()
            for protocol in list(self._protocols):
                if protocol._aura_transport is not None: protocol._aura_transport.close()
            tasks = (self.tasks | self._relay_tasks) - {asyncio.current_task()}
            for task in tasks: task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks,timeout=2)
                for task in done:
                    if not task.cancelled(): task.exception()
                # The duplex owner may have been cancelled during its own
                # finally block. A completed relay is safe to release even if
                # that owner never reached its normal ownership bookkeeping.
                self._release_finished_relays(done)
                if pending: raise BrowserNetworkError('BROWSER_PROXY_CLEANUP_UNCONFIRMED')
            waits = {protocol._aura_closed for protocol in self._protocols}
            if waits:
                _, pending = await asyncio.wait(waits,timeout=2)
                if pending: raise BrowserNetworkError('BROWSER_PROXY_CLEANUP_UNCONFIRMED')
            for writer in writers:
                try:
                    with suppress(OSError,ConnectionError): await asyncio.wait_for(writer.wait_closed(),timeout=1)
                except asyncio.TimeoutError:
                    raise BrowserNetworkError('BROWSER_PROXY_CLEANUP_UNCONFIRMED') from None
                self.writers.discard(writer)
            if self.tasks or self._relay_tasks or self._protocols:
                raise BrowserNetworkError('BROWSER_PROXY_CLEANUP_UNCONFIRMED')
            self._close_complete=True
            if self._handler_failed: raise BrowserNetworkError('BROWSER_PROXY_HANDLER_FAILED')
