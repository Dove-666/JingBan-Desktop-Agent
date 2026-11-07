"""Real Windows file sharing on a named, owned temporary browser profile."""
import asyncio
import ctypes
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_session import BrowserSession, BrowserError


@unittest.skipUnless(sys.platform == "win32", "Windows sharing semantics required")
class ProfileCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_prefix_profile_is_retained_before_first_cleanup(self):
        browser = BrowserSession()
        profile = tempfile.TemporaryDirectory(prefix='aura-owned-negative-')
        path = Path(profile.name).resolve()
        browser._profile = profile
        try:
            with self.assertRaisesRegex(BrowserError, 'BROWSER_CLEANUP_UNCONFIRMED'):
                await browser.close()
            self.assertTrue(path.is_dir())
            self.assertIs(browser._profile, profile)
            self.assertFalse(browser._close_complete)
        finally:
            # The test created and verified this exact directory. Production
            # close must leave it untouched because its owner prefix is wrong.
            self.assertEqual(path.parent, Path(tempfile.gettempdir()).resolve())
            profile.cleanup()
            browser._profile = None
            await browser.close()

    def lock(self, path):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        return kernel, handle

    def profile(self):
        profile = tempfile.TemporaryDirectory(prefix="aura-isolated-browser-")
        path = Path(profile.name).resolve()
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), path.parent)
        self.assertFalse(path.is_symlink())
        (path / "owned-cache").write_bytes(b"SYNTHETIC-OWNED-CACHE")
        return profile, path

    async def test_transient_owned_cache_lock_can_finish_close_without_replaying_actions(self):
        browser = BrowserSession(); profile, path = self.profile(); browser._profile = profile
        kernel, handle = self.lock(path / "owned-cache")
        async def release():
            await asyncio.sleep(.2)
            self.assertTrue(kernel.CloseHandle(handle))
        releasing = asyncio.create_task(release())
        try:
            await browser.close()
            self.assertTrue(releasing.done(), "Cleanup was reported before the owned sharing lock released")
            self.assertTrue(browser._close_complete)
            self.assertIsNone(browser._profile)
            self.assertFalse(path.exists())
            self.assertTrue(browser.proxy._close_complete)
        finally:
            await releasing
            await browser.close()

    async def test_persistent_owned_cache_lock_remains_unconfirmed_and_can_retry_cleanup_only(self):
        browser = BrowserSession(); profile, path = self.profile(); browser._profile = profile
        kernel, handle = self.lock(path / "owned-cache")
        try:
            with self.assertRaisesRegex(BrowserError, "BROWSER_CLEANUP_UNCONFIRMED"):
                await asyncio.wait_for(browser.close(), timeout=3)
            self.assertFalse(browser._close_complete)
            self.assertIs(profile, browser._profile)
            self.assertTrue(path.exists())
            self.assertTrue(browser.proxy._close_complete)
        finally:
            self.assertTrue(kernel.CloseHandle(handle))
            await browser.close()
        self.assertFalse(path.exists())

    async def test_unconfirmed_process_never_deletes_the_profile(self):
        browser = BrowserSession(); profile, path = self.profile(); browser._profile = profile
        class Process:
            returncode = None
            async def wait(self): raise OSError("SYNTHETIC-WAIT-UNCONFIRMED")
        process = Process(); browser._process = process
        try:
            with self.assertRaisesRegex(BrowserError, "BROWSER_CLEANUP_UNCONFIRMED"):
                await browser.close()
            self.assertIs(profile, browser._profile)
            self.assertTrue((path / "owned-cache").is_file())
        finally:
            process.returncode = 0
            await browser.close()
        self.assertFalse(path.exists())
