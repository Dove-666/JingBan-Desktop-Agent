"""Repeated shutdown cancellation must keep accurate ownership, without I/O."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_network import BrowserNetworkError, PinnedPublicProxy, PublicBrowserPolicy


class RelayCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_child_error_during_repeated_cancel_remains_reported(self):
        for failure, unexpected in ((RuntimeError('owned-fixture-error'), True),
                                    (ConnectionResetError('owned-fixture-reset'), False)):
            proxy = PinnedPublicProxy(PublicBrowserPolicy())
            started, joining = asyncio.Event(), asyncio.Event()
            children = []
            async def pipe(*_):
                children.append(asyncio.current_task())
                index = len(children)
                if index == 2: started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    if index == 1: raise failure
                    joining.set()
                    await asyncio.Event().wait()
            with self.subTest(unexpected=unexpected), patch.object(proxy, '_pipe', side_effect=pipe):
                owner = asyncio.create_task(proxy._duplex(None, None, None, None))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    owner.cancel()
                    await asyncio.wait_for(joining.wait(), 1)
                    owner.cancel()
                    await asyncio.gather(owner, return_exceptions=True)
                    self.assertEqual(proxy._relay_tasks, set())
                    self.assertEqual(proxy._handler_failed, unexpected)
                    if unexpected:
                        for _ in range(2):
                            with self.assertRaisesRegex(BrowserNetworkError, 'BROWSER_PROXY_HANDLER_FAILED'):
                                await proxy.close()
                    else:
                        await proxy.close()
                        self.assertTrue(proxy._close_complete)
                finally:
                    owner.cancel()
                    for task in children: task.cancel()
                    await asyncio.gather(owner, *children, return_exceptions=True)

    async def test_second_cancel_during_child_join_does_not_leave_finished_relays(self):
        proxy = PinnedPublicProxy(PublicBrowserPolicy())
        started, joining = asyncio.Event(), asyncio.Event()
        children = []
        async def pipe(*_):
            children.append(asyncio.current_task())
            if len(children) == 2: started.set()
            try:
                await asyncio.Event().wait()
            finally:
                joining.set()
                await asyncio.Event().wait()
        with patch.object(proxy, '_pipe', side_effect=pipe):
            owner = asyncio.create_task(proxy._duplex(None, None, None, None))
            try:
                await asyncio.wait_for(started.wait(), 1)
                owner.cancel()
                await asyncio.wait_for(joining.wait(), 1)
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
                self.assertTrue(all(task.done() for task in children))
                self.assertEqual(proxy._relay_tasks, set())
                await proxy.close()
                self.assertTrue(proxy._close_complete)
            finally:
                owner.cancel()
                for task in children: task.cancel()
                await asyncio.gather(owner, *children, return_exceptions=True)

    async def test_unjoined_relay_stays_owned_until_confirmed_finished(self):
        proxy = PinnedPublicProxy(PublicBrowserPolicy())
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_cleanup():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
        task = asyncio.create_task(slow_cleanup())
        proxy._relay_tasks.add(task)
        try:
            await asyncio.wait_for(started.wait(), 1)
            async def not_joined(tasks, timeout):
                self.assertEqual(timeout, 2)
                await asyncio.sleep(0)
                return set(), set(tasks)
            with patch('browser_network.asyncio.wait', side_effect=not_joined):
                with self.assertRaisesRegex(BrowserNetworkError, 'BROWSER_PROXY_CLEANUP_UNCONFIRMED'):
                    await proxy.close()
            self.assertFalse(task.done())
            self.assertIn(task, proxy._relay_tasks)
            self.assertFalse(proxy._close_complete)
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await proxy.close()
            self.assertEqual(proxy._relay_tasks, set())
            self.assertTrue(proxy._close_complete)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
