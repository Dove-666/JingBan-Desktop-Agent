"""Plain-HTTP response header/body ownership and no-retry failure boundaries."""
import asyncio
import unittest
from unittest.mock import patch

from agent_backend.nanobot.http_response import HttpResponseState, relay_final_http_response, GATEWAY_UNKNOWN
from agent_backend.nanobot.browser_network import PinnedPublicProxy, PublicBrowserPolicy, BrowserNetworkError


class Writer:
    def __init__(self): self.data = bytearray(); self.closed = False
    def write(self, raw): self.data.extend(raw)
    async def drain(self): pass
    def is_closing(self): return self.closed
    def close(self): self.closed = True
    async def wait_closed(self): pass


class HttpResponseTests(unittest.IsolatedAsyncioTestCase):
    async def relay(self, raw, **kwargs):
        reader, writer, state = asyncio.StreamReader(), Writer(), HttpResponseState()
        reader.feed_data(raw); reader.feed_eof()
        body_calls = []
        async def body(source, target): body_calls.append(True); target.write(await source.read())
        result = await relay_final_http_response(reader, writer, body, state=state, **kwargs)
        return result, bytes(writer.data), body_calls, state

    async def test_final_headers_and_body_and_interim_headers_forward_once(self):
        raw = b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Hints\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nBODY"
        success, output, calls, state = await self.relay(raw)
        self.assertTrue(success and state.final_headers_forwarded)
        self.assertEqual(raw, output); self.assertEqual([True], calls)

    async def test_pre_final_failure_has_only_explicit_502_and_never_forwards_interim_or_partial(self):
        for raw in (b"", b"HTTP/1.1 200 OK\r\nContent-Length:", b"HTTP/1.1 103 Hints\r\n\r\n",
                    b"HTTP/1.1 100 Continue\r\n\r\n" * 9,
                    b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 32768 + b"\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n"):
            with self.subTest(prefix=raw[:20]):
                success, output, calls, state = await self.relay(raw)
                self.assertFalse(success); self.assertEqual(GATEWAY_UNKNOWN, output)
                self.assertEqual([], calls); self.assertTrue(state.final_headers_forwarded)

    async def test_timeout_emits_explicit_unknown_but_cancellation_is_unchanged(self):
        reader, writer, state = asyncio.StreamReader(), Writer(), HttpResponseState()
        async def body(*_): raise AssertionError("No body before final headers")
        self.assertFalse(await relay_final_http_response(reader, writer, body, state=state, timeout=.02))
        self.assertEqual(GATEWAY_UNKNOWN, writer.data)
        writer, state = Writer(), HttpResponseState()
        task = asyncio.create_task(relay_final_http_response(reader, writer, body, state=state))
        await asyncio.sleep(0); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(state.final_headers_forwarded); self.assertEqual(b"", writer.data)

    async def test_outer_proxy_never_appends_403_after_final_header_on_body_timeout_or_limit(self):
        for failure in (asyncio.TimeoutError(), BrowserNetworkError("BROWSER_PROXY_TRANSFER_LIMIT")):
            with self.subTest(failure=type(failure).__name__):
                incoming, remote_reader = asyncio.StreamReader(), asyncio.StreamReader()
                incoming.feed_data(b"POST http://127.0.0.1:12345/owned HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 1\r\n\r\nx")
                incoming.feed_eof()
                remote_reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"); remote_reader.feed_eof()
                outgoing, remote = Writer(), Writer()
                proxy = PinnedPublicProxy(PublicBrowserPolicy(allowed_test_hosts={("127.0.0.1", 12345)}))
                async def connect(*_): return remote_reader, remote
                async def body(*_): outgoing.write(b"PARTIAL"); raise failure
                with patch("agent_backend.nanobot.browser_network.asyncio.open_connection", connect), patch.object(proxy, "_pipe", body):
                    await proxy._accept(incoming, outgoing)
                self.assertEqual(1, bytes(remote.data).count(b"POST /owned HTTP/1.1"))
                self.assertEqual(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nPARTIAL", outgoing.data)
                self.assertNotIn(b"403", outgoing.data)
                self.assertTrue(outgoing.closed and remote.closed)
                self.assertFalse(proxy.tasks or proxy.writers)
                await proxy.close()

    async def test_connect_keeps_its_original_duplex_path_without_response_header_parser(self):
        incoming, remote_reader = asyncio.StreamReader(), asyncio.StreamReader()
        incoming.feed_data(b"CONNECT 127.0.0.1:12345 HTTP/1.1\r\nHost: 127.0.0.1:12345\r\n\r\n"); incoming.feed_eof()
        outgoing, remote, calls = Writer(), Writer(), []
        proxy = PinnedPublicProxy(PublicBrowserPolicy(allowed_test_hosts={("127.0.0.1", 12345)}))
        async def connect(*_): return remote_reader, remote
        async def duplex(*_): calls.append(True)
        async def forbidden(*_): raise AssertionError("CONNECT must not parse encrypted HTTP")
        with patch("agent_backend.nanobot.browser_network.asyncio.open_connection", connect), \
             patch.object(proxy, "_duplex", duplex), patch.object(proxy, "_relay_http_response", forbidden):
            await proxy._accept(incoming, outgoing)
        self.assertEqual([True], calls)
        self.assertEqual(b"HTTP/1.1 200 Connection Established\r\n\r\n", outgoing.data)
        self.assertTrue(outgoing.closed and remote.closed)
        await proxy.close()


if __name__ == "__main__": unittest.main()
