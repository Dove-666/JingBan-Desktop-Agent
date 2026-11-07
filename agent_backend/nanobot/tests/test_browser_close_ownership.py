"""Deterministic owned proxy shutdown races, no user profile/network/model."""
import asyncio
import socket
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_network import PinnedPublicProxy, PublicBrowserPolicy
from browser_session import BrowserSession, BrowserError


class BrowserCloseOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_joins_inflight_startup_instead_of_returning_before_late_spawn(self):
        browser = BrowserSession(executable=Path(__file__))
        entered = asyncio.Event()
        async def creating(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('browser_session.asyncio.create_subprocess_exec', side_effect=creating):
            startup = asyncio.create_task(browser._ensure_started())
            try:
                await asyncio.wait_for(entered.wait(),timeout=1)
                await asyncio.wait_for(browser.close(),timeout=2)
                self.assertTrue(startup.done())
                self.assertTrue(startup.cancelled())
                self.assertTrue(browser._close_complete)
                self.assertTrue(browser.proxy._close_complete)
                self.assertIsNone(browser._profile)
            finally:
                startup.cancel()
                await asyncio.gather(startup,return_exceptions=True)
                await browser.close()

    async def test_session_retains_failed_cleanup_for_explicit_retry_and_still_closes_proxy(self):
        browser = BrowserSession()
        class Connection:
            failed = True
            async def call(self, *_): pass
            async def close(self):
                if self.failed: raise OSError('owned fixture failure')
        connection = Connection()
        browser._connection = connection
        with self.assertRaisesRegex(BrowserError, 'BROWSER_CLEANUP_UNCONFIRMED'):
            await browser.close()
        self.assertIs(browser._connection, connection)
        self.assertTrue(browser.proxy._close_complete)
        self.assertFalse(browser._close_complete)
        connection.failed = False
        await browser.close()
        self.assertTrue(browser._close_complete)
        self.assertIsNone(browser._connection)

    async def test_profile_cleanup_failure_is_not_silently_forgotten(self):
        browser = BrowserSession()
        class Profile:
            failed = True
            def cleanup(self):
                if self.failed: raise OSError('owned fixture sharing violation')
        profile = Profile()
        browser._profile = profile
        with self.assertRaisesRegex(BrowserError, 'BROWSER_CLEANUP_UNCONFIRMED'):
            await browser.close()
        self.assertIs(browser._profile, profile)
        self.assertFalse(browser._close_complete)
        profile.failed = False
        await browser.close()
        self.assertIsNone(browser._profile)
        self.assertTrue(browser._close_complete)

    async def test_pending_native_accept_and_burst_clients_close_without_owned_residue(self):
        for size in (0, 1, 12):
            proxy = PinnedPublicProxy(PublicBrowserPolicy())
            port = await proxy.start()
            clients = []
            try:
                clients = await asyncio.gather(*(asyncio.open_connection('127.0.0.1', port) for _ in range(size)))
                await proxy.close()
                self.assertFalse(proxy.server.is_serving())
                self.assertEqual(proxy.server.sockets, ())
                self.assertTrue(proxy._accept_task.done())
                self.assertIsNone(proxy._accept_future)
                self.assertEqual(proxy._protocols, set())
                self.assertEqual(proxy.tasks, set())
                self.assertEqual(proxy.writers, set())
                self.assertEqual(proxy._relay_tasks, set())
                await proxy.close()
            finally:
                for _, writer in clients: writer.close()
                await asyncio.gather(*(writer.wait_closed() for _, writer in clients), return_exceptions=True)
                await proxy.close()

    async def test_active_connect_tunnel_close_joins_both_pipes_and_upstream_writer(self):
        loop = asyncio.get_running_loop()
        upstream_done = loop.create_future()
        async def upstream(reader, writer):
            try: await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                upstream_done.set_result(True)
        origin = await asyncio.start_server(upstream, '127.0.0.1', 0)
        origin_port = origin.sockets[0].getsockname()[1]
        proxy = PinnedPublicProxy(PublicBrowserPolicy(allowed_test_hosts={('127.0.0.1', origin_port)}))
        writer = None
        try:
            port = await proxy.start()
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(f'CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n\r\n'.encode())
            await writer.drain()
            self.assertIn(b'200', await reader.readuntil(b'\r\n\r\n'))
            await proxy.close()
            self.assertTrue(await asyncio.wait_for(upstream_done, timeout=2))
            self.assertEqual(proxy._relay_tasks, set())
            self.assertEqual(proxy.tasks, set())
            self.assertEqual(proxy.writers, set())
        finally:
            if writer is not None:
                writer.close()
                await asyncio.gather(writer.wait_closed(), return_exceptions=True)
            await proxy.close()
            origin.close()
            await origin.wait_closed()

    async def test_cancelling_duplex_joins_both_owned_pipes(self):
        proxy = PinnedPublicProxy(PublicBrowserPolicy())
        entered = asyncio.Event()
        children = []
        async def blocked(*_):
            children.append(asyncio.current_task())
            if len(children) == 2: entered.set()
            await asyncio.Event().wait()
        with patch.object(proxy, "_pipe", side_effect=blocked):
            task = asyncio.create_task(proxy._duplex(None, None, None, None))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(all(child.done() for child in children), "owned duplex pipes escaped cancellation")
            finally:
                for child in children: child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                await proxy.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows Proactor ownership race")
    async def test_completed_accept_then_close_never_attaches_to_a_closed_server_or_leaks_socket(self):
        loop = asyncio.get_running_loop()
        proxy = PinnedPublicProxy(PublicBrowserPolicy())
        accepted = asyncio.Event()
        pending = loop.create_future()
        errors = []
        old_handler = loop.get_exception_handler()
        # Capture, assert, then restore the application's handler. Production
        # code must never install an exception-swallowing handler.
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        left, right = socket.socketpair()
        try:
            def accept(_sock):
                accepted.set()
                return pending
            with patch.object(loop._proactor, "accept", side_effect=accept):
                await proxy.start()
                await asyncio.wait_for(accepted.wait(), timeout=1)
                # The kernel completed accept, but its Python continuation has
                # not run. Server.close cannot cancel an already-done future.
                pending.set_result((left, ("127.0.0.1", 12345)))
                await proxy.close()
            self.assertEqual(errors, [], "late accept attached to a closed asyncio.Server")
            self.assertEqual(left.fileno(), -1, "accepted socket lost in the close/completion gap")
            self.assertEqual(proxy.tasks, set())
            self.assertEqual(proxy.writers, set())
        finally:
            left.close(); right.close()
            loop.set_exception_handler(old_handler)
            await proxy.close()


if __name__ == "__main__": unittest.main()
