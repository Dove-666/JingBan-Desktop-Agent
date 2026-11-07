"""Fake Windows metadata swap, without touching any real drive or junction."""
import contextlib
import json
import stat
from types import SimpleNamespace
import unittest

from agent_backend.nanobot.windows_system import WindowsSystem


class WindowsDirectoryPathSwapReviewTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, replacement_attributes):
        calls = []
        target_stats = []

        def metadata(attributes=0):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=attributes)

        def lstat(path):
            text = str(path)
            calls.append(("lstat", text))
            if text == "C:\\":
                return metadata()
            self.assertEqual(text, "C:\\owned_qa")
            # First validation sees the original directory. The next explicit
            # metadata lookup already sees the replacement junction flag.
            target_stats.append(text)
            return metadata(0 if len(target_stats) == 1 else replacement_attributes)

        class OutsideEntry:
            name = "OUTSIDE_METADATA_CANARY.txt"
            path = "C:\\owned_qa\\OUTSIDE_METADATA_CANARY.txt"

            def stat(self, *, follow_symlinks):
                assert follow_symlinks is False
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0, st_size=4096)

        @contextlib.contextmanager
        def scandir(path):
            calls.append(("scandir", str(path)))
            yield iter([OutsideEntry()])

        system = WindowsSystem(fixed_drive=lambda root: root == "C:\\", lstat=lstat, scandir=scandir)
        result = json.loads(await system.inspect_directory("C:\\owned_qa"))
        return {"calls": calls, "result": result, "target_lstat_calls": len(target_stats),
                "replacement_attributes": replacement_attributes}

    async def test_directory_reparse_detected_by_second_lstat_is_not_enumerated(self):
        self.observations = []
        for flag in (0x400, 0x1000, 0x40000, 0x400000):
            with self.subTest(flag=hex(flag)):
                observation = await self.probe(flag)
                self.observations.append(observation)
                result, calls = observation["result"], observation["calls"]
                self.assertEqual([call for call in calls if call[0] == "scandir"], [],
                                 "Second lstat already exposed an unsafe directory; do not enumerate")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["error"], "reparse_or_offline_path")
                self.assertEqual(result["entries"], [])

    async def test_unchanged_directory_still_returns_observed_metadata(self):
        self.observation = await self.probe(0)
        self.assertEqual(self.observation["target_lstat_calls"], 2)
        self.assertEqual(len([call for call in self.observation["calls"] if call[0] == "scandir"]), 1)
        result = self.observation["result"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["entries"], [{"name": "OUTSIDE_METADATA_CANARY.txt", "kind": "file",
                         "logical_bytes": 4096, "metadata_status": "observed"}])


if __name__ == "__main__":
    unittest.main()
