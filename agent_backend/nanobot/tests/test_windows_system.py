"""Read-only observation boundaries and bounded/partial metadata measurements."""
from __future__ import annotations

import asyncio
from collections import namedtuple
import contextlib
import json
import ntpath
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_system import WindowsSystem, _Budget, _memory_status


Usage = namedtuple("Usage", "total used free")


def metadata(mode=stat.S_IFREG, size=10, modified=0, attributes=0):
    return SimpleNamespace(st_mode=mode, st_size=size, st_mtime=modified,
                           st_file_attributes=attributes)


class Entry:
    def __init__(self, parent, name, info=None, failure=None, on_stat=None):
        self.path = ntpath.join(parent, name)
        self.name = name
        self.info, self.failure, self.on_stat = info or metadata(), failure, on_stat

    def stat(self, *, follow_symlinks):
        assert follow_symlinks is False
        if self.on_stat:
            self.on_stat()
        if self.failure:
            raise self.failure()
        return self.info


class Tree:
    def __init__(self):
        self.folders = {"C:\\qa": []}
        self.folder_metadata = {}
        self.failures = {}
        self.scan_calls = []
        self.stat_calls = []

    def lstat(self, path):
        path = str(path)
        self.stat_calls.append(path)
        if path in self.failures:
            raise self.failures[path]()
        if path == "C:\\" or path in self.folders:
            return self.folder_metadata.get(path, metadata(stat.S_IFDIR))
        raise FileNotFoundError()

    @contextlib.contextmanager
    def scandir(self, path):
        path = str(path)
        self.scan_calls.append(path)
        if path in self.failures:
            raise self.failures[path]()
        yield iter(self.folders[path])

    def system(self, **kwargs):
        return WindowsSystem(disk_usage=lambda root: Usage(1000, 600, 400),
                             fixed_drive=lambda root: root == "C:\\",
                             categories_provider=lambda root: [{"category": "fixture", "path": "C:\\qa",
                                                                 "recommendation": "review_only"}],
                             lstat=self.lstat, scandir=self.scandir,
                             wall_clock=lambda: 2_000_000, **kwargs)


class DiskObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_capacity_and_known_file_sizes_without_cleanup_claims(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "old.tmp", metadata(size=12)),
                                    Entry("C:\\qa", "recent.tmp", metadata(size=9, modified=1_999_999))]
        result = json.loads(await tree.system().inspect_disk_usage("C:"))
        self.assertEqual(result["disk"], {"drive": "C:\\", "total_bytes": 1000, "used_bytes": 600, "free_bytes": 400})
        self.assertEqual(result["status"], "complete")
        item = result["categories"][0]
        self.assertEqual(item["measured_bytes"], 21)
        self.assertEqual(item["older_than_7_days_bytes"], 12)
        self.assertIsNone(result["reclaimable_bytes"])
        self.assertIsNone(item["reclaimable_bytes"])
        self.assertEqual(result["deleted_files"], 0)
        self.assertFalse(result["file_contents_read"])

    async def test_entry_limit_is_shared_across_categories_and_lower_bounds_are_explicit(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", str(i), metadata(size=10)) for i in range(5)]
        system = tree.system(max_entries=2)
        system.categories_provider = lambda root: [{"category": "first", "path": "C:\\qa"},
                                                   {"category": "later", "path": "C:\\later"}]
        result = json.loads(await system.inspect_disk_usage("C"))
        self.assertEqual(result["entries_examined"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["categories"][0]["measured_bytes"], 20)
        self.assertEqual(result["categories"][0]["incomplete_reasons"], ["entry_limit"])
        self.assertEqual(result["categories"][1]["scan_status"], "not_scanned")
        self.assertIsNone(result["categories"][1]["measured_bytes"])
        self.assertNotIn("C:\\later", tree.stat_calls)

    async def test_empty_readable_directory_is_complete_zero(self):
        result = json.loads(await Tree().system().inspect_disk_usage("C:/"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["categories"][0]["measured_bytes"], 0)

    async def test_denied_location_is_unknown_never_zero(self):
        tree = Tree()
        tree.failures["C:\\qa"] = PermissionError
        result = json.loads(await tree.system().inspect_disk_usage("C"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["categories"][0]["scan_status"], "unavailable")
        self.assertEqual(result["categories"][0]["incomplete_reasons"], ["permission_denied"])
        self.assertIsNone(result["categories"][0]["measured_bytes"])

    async def test_mid_scan_permission_failure_retains_lower_bound_and_marks_partial(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "visible", metadata(size=15)),
                                    Entry("C:\\qa", "denied", failure=PermissionError)]
        result = json.loads(await tree.system().inspect_disk_usage("C"))
        item = result["categories"][0]
        self.assertEqual(item["scan_status"], "partial")
        self.assertEqual(item["measured_bytes"], 15)
        self.assertEqual(item["denied_entries"], 1)

    async def test_missing_category_is_distinguished_from_measured_zero(self):
        tree = Tree()
        tree.failures["C:\\qa"] = FileNotFoundError
        result = json.loads(await tree.system().inspect_disk_usage("C"))
        self.assertEqual(result["categories"][0]["scan_status"], "missing")
        self.assertIsNone(result["categories"][0]["measured_bytes"])

    async def test_reparse_offline_recall_and_symbolic_entries_are_not_followed_or_counted(self):
        tree = Tree()
        for index, attrs in enumerate([0x400, 0x1000, 0x40000, 0x400000]):
            tree.folders["C:\\qa"].append(Entry("C:\\qa", f"blocked{index}", metadata(stat.S_IFDIR, attributes=attrs)))
        tree.folders["C:\\qa"].append(Entry("C:\\qa", "link", metadata(stat.S_IFLNK, size=999)))
        result = json.loads(await tree.system().inspect_disk_usage("C"))
        self.assertEqual(tree.scan_calls, ["C:\\qa"])
        self.assertEqual(result["categories"][0]["skipped_entries"], 5)
        self.assertIsNone(result["categories"][0]["measured_bytes"])

    async def test_queued_directory_is_checked_again_before_recursion(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "changed", metadata(stat.S_IFDIR))]
        tree.folders["C:\\qa\\changed"] = []
        tree.folder_metadata["C:\\qa\\changed"] = metadata(stat.S_IFDIR, attributes=0x400)
        result = json.loads(await tree.system().inspect_disk_usage("C"))
        self.assertEqual(tree.scan_calls, ["C:\\qa"])
        self.assertEqual(result["status"], "partial")

    async def test_cancellation_stops_before_next_entry(self):
        cancel = threading.Event()
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "first", metadata(size=8), on_stat=cancel.set),
                                    Entry("C:\\qa", "never_read", metadata(size=500))]
        result = json.loads(await tree.system(cancel=cancel).inspect_disk_usage("C"))
        self.assertEqual(result["entries_examined"], 1)
        self.assertEqual(result["categories"][0]["measured_bytes"], 8)
        self.assertIn("cancelled", result["categories"][0]["incomplete_reasons"])

    async def test_pre_cancelled_scan_does_not_enumerate_files(self):
        cancel = threading.Event()
        cancel.set()
        tree = Tree()
        result = json.loads(await tree.system(cancel=cancel).inspect_disk_usage("C"))
        self.assertEqual(tree.scan_calls, [])
        self.assertEqual(result["categories"][0]["scan_status"], "not_scanned")
        self.assertIsNone(result["categories"][0]["measured_bytes"])

    async def test_time_budget_is_global_and_does_not_report_unscanned_as_zero(self):
        tick = [0.0]
        def clock():
            tick[0] += .025
            return tick[0]
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", str(i)) for i in range(100)]
        result = json.loads(await tree.system(clock=clock, time_budget_seconds=.10).inspect_disk_usage("C"))
        self.assertLess(result["entries_examined"], 100)
        self.assertIn("time_limit", result["categories"][0]["incomplete_reasons"])
        self.assertEqual(result["status"], "partial")

    async def test_default_drive_does_not_expose_environment(self):
        with patch.dict(os.environ, {"SystemDrive": "C:"}):
            result = json.loads(await Tree().system().inspect_disk_usage())
        self.assertEqual(result["disk"]["drive"], "C:\\")
        self.assertNotIn("environment", result)

    async def test_network_device_invalid_drive_and_nonfixed_volume_never_scan(self):
        tree = Tree()
        for drive in ["\\\\server\\share", "\\\\?\\C:\\", "C:\\Windows", "D:", "C:secret", None, ""]:
            system = tree.system()
            if drive == "":
                system.fixed_drive = lambda value: False
            result = json.loads(await system.inspect_disk_usage(drive))
            self.assertEqual(result["status"], "unavailable", drive)
        self.assertEqual(tree.scan_calls, [])


class DiskCategoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def registered(self, system):
        try:
            from nanobot.agent.tools.base import Tool
            from nanobot.agent.tools.registry import ToolRegistry
        except ModuleNotFoundError as error:
            if error.name == "nanobot":
                self.skipTest("Run registered-tool checks with the adjacent backend SDK interpreter")
            raise
        from tool_catalog import CATALOG
        spec = CATALOG["inspect_disk_usage"]
        calls = []

        class RegisteredDiskTool(Tool):
            @property
            def name(self):
                return spec.name

            @property
            def description(self):
                return spec.description

            @property
            def parameters(self):
                return spec.parameters

            @property
            def read_only(self):
                return spec.read_only

            async def execute(self, **arguments):
                calls.append(dict(arguments))
                return await system.inspect_disk_usage(**arguments)

        registry = ToolRegistry()
        registry.register(RegisteredDiskTool())
        return registry, calls, spec

    async def test_registered_schema_accepts_fixed_categories_and_rejects_unknown_arguments(self):
        tree = Tree()
        registry, calls, spec = self.registered(tree.system())
        schema = registry.get_definitions()[0]["function"]["parameters"]
        expected = ["all", "user_temporary_files", "system_temporary_files", "recycle_bin", "downloads",
                    "windows_update_downloads", "delivery_optimization_cache"]
        self.assertEqual(schema["properties"]["category"]["enum"], expected)
        self.assertEqual(schema["required"], [])
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(spec.read_only)
        self.assertEqual(spec.effect, "none")
        self.assertFalse(spec.requires_gui)
        self.assertTrue(spec.available(tools_enabled=True, computer_enabled=False, gui_configured=False, platform="win32"))
        for category in ("", "unknown", "C:\\owned_fake", None, True, 1, [], {}):
            with self.subTest(category=category):
                result = await registry.execute("inspect_disk_usage", {"drive": "C:", "category": category})
                self.assertTrue(result.is_error)
        self.assertEqual(calls, [])
        self.assertEqual(tree.scan_calls, [])

    async def test_registered_target_recovers_downloads_after_default_temp_budget_is_exhausted(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", str(i), metadata(size=10)) for i in range(5)]
        tree.folders["C:\\later"] = [Entry("C:\\later", "archive.iso", metadata(size=12 * 1024**3))]
        system = tree.system(max_entries=2)
        system.categories_provider = lambda root: [
            {"category": "user_temporary_files", "path": "C:\\qa"},
            {"category": "downloads", "path": "C:\\later"}]
        registry, calls, _ = self.registered(system)
        default = json.loads(await registry.execute("inspect_disk_usage", {"drive": "C:"}))
        explicit = await registry.execute("inspect_disk_usage", {"drive": "C:", "category": "all"})
        self.assertFalse(getattr(explicit, "is_error", False), explicit)
        self.assertEqual(default, json.loads(explicit))
        self.assertEqual(default["used_category"], "all")
        self.assertEqual(default["available_categories"], ["user_temporary_files", "downloads"])
        self.assertEqual(default["status"], "partial")
        self.assertEqual(default["entries_examined"], 2)
        self.assertEqual(default["categories"][0]["measured_bytes"], 20)
        self.assertEqual(default["categories"][1]["scan_status"], "not_scanned")
        self.assertIsNone(default["categories"][1]["measured_bytes"])
        self.assertIsNone(default["reclaimable_bytes"])
        tree.scan_calls.clear()
        selected = await registry.execute("inspect_disk_usage", {"drive": "C:", "category": "downloads"})
        self.assertFalse(getattr(selected, "is_error", False), selected)
        selected = json.loads(selected)
        self.assertEqual(tree.scan_calls, ["C:\\later"])
        self.assertEqual(selected["used_category"], "downloads")
        self.assertEqual(selected["available_categories"], default["available_categories"])
        self.assertEqual(selected["status"], "complete")
        self.assertEqual(selected["entries_examined"], 1)
        self.assertEqual([item["category"] for item in selected["categories"]], ["downloads"])
        self.assertEqual(selected["categories"][0]["measured_bytes"], 12 * 1024**3)
        self.assertTrue(selected["read_only"])
        self.assertFalse(selected["cleanup_performed"])
        self.assertFalse(selected["file_contents_read"])
        self.assertEqual(selected["deleted_files"], 0)
        self.assertEqual(calls[-1], {"drive": "C:", "category": "downloads"})

    async def test_direct_invalid_or_unavailable_category_is_rejected_without_enumeration(self):
        tree = Tree()
        system = tree.system()
        for category in ("", "unknown", "C:\\owned_fake", None, True, 1, [], {}, "downloads"):
            with self.subTest(category=category):
                result = json.loads(await system.inspect_disk_usage("C:", category=category))
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["error"], "invalid_category")
                self.assertEqual(result["categories"], [])
                self.assertIsNone(result["used_category"])
                self.assertEqual(result["available_categories"], ["fixture"])
        self.assertEqual(tree.scan_calls, [])

    async def test_selected_category_retains_unsafe_path_denial_and_cancellation(self):
        tree = Tree()
        system = tree.system()
        system.categories_provider = lambda root: [{"category": "downloads", "path": "C:\\qa"}]
        tree.folder_metadata["C:\\qa"] = metadata(stat.S_IFDIR, attributes=0x400)
        result = json.loads(await system.inspect_disk_usage("C:", category="downloads"))
        self.assertEqual(result["categories"][0]["scan_status"], "unavailable")
        self.assertEqual(result["categories"][0]["incomplete_reasons"], ["reparse_or_offline_path"])
        self.assertEqual(tree.scan_calls, [])
        tree.folder_metadata.clear()
        system.cancel.set()
        result = json.loads(await system.inspect_disk_usage("C:", category="downloads"))
        self.assertEqual(result["used_category"], "downloads")
        self.assertEqual(result["categories"][0]["scan_status"], "not_scanned")
        self.assertEqual(result["categories"][0]["incomplete_reasons"], ["cancelled"])
        self.assertEqual(tree.scan_calls, [])


class DirectoryObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_listing_is_non_recursive_and_bounded(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "folder", metadata(stat.S_IFDIR)),
                                    Entry("C:\\qa", "one", metadata(size=23)), Entry("C:\\qa", "two")]
        result = json.loads(await tree.system().inspect_directory("C:\\qa", limit=2))
        self.assertEqual(result["status"], "partial")
        self.assertIn("result_limit", result["incomplete_reasons"])
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(tree.scan_calls, ["C:\\qa"])
        self.assertFalse(result["recursive"])

    async def test_directory_does_not_open_reparse_or_offline_ancestor(self):
        tree = Tree()
        tree.folder_metadata["C:\\qa"] = metadata(stat.S_IFDIR, attributes=0x1000)
        result = json.loads(await tree.system().inspect_directory("C:\\qa\\nested"))
        self.assertEqual(result["error"], "reparse_or_offline_path")
        self.assertEqual(tree.scan_calls, [])

    async def test_cloud_metadata_is_labelled_without_reading_file_contents(self):
        tree = Tree()
        tree.folders["C:\\qa"] = [Entry("C:\\qa", "cloud", metadata(size=9999, attributes=0x400000))]
        result = json.loads(await tree.system().inspect_directory("C:\\qa"))
        self.assertEqual(result["entries"][0]["kind"], "unavailable")
        self.assertIsNone(result["entries"][0]["logical_bytes"])

    async def test_unc_device_ads_relative_reserved_names_and_invalid_limits_are_rejected(self):
        tree = Tree()
        for path in ["\\\\server\\share", "\\\\?\\C:\\qa", "C:qa", "qa", "C:\\qa:stream", "C:\\CON", "C:\\qa\\NUL.txt", "C:\\qa*", "C:\\qa\x00"]:
            result = json.loads(await tree.system().inspect_directory(path))
            self.assertEqual(result["status"], "unavailable", path)
        for limit in [0, 201, True, 1.5, "2"]:
            result = json.loads(await tree.system().inspect_directory("C:\\qa", limit))
            self.assertEqual(result["error"], "invalid_limit")
        self.assertEqual(tree.scan_calls, [])

    async def test_missing_and_permission_denied_are_explicit(self):
        tree = Tree()
        result = json.loads(await tree.system().inspect_directory("C:\\missing"))
        self.assertEqual(result["error"], "not_found")
        tree.failures["C:\\qa"] = PermissionError
        result = json.loads(await tree.system().inspect_directory("C:\\qa"))
        self.assertEqual(result["error"], "permission_denied")


class SystemObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_memory_remains_unknown_with_partial_status(self):
        result = json.loads(await WindowsSystem(memory_status=lambda: None, drive_roots=lambda: []).inspect_system())
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["memory"])
        self.assertEqual(result["incomplete_reasons"], ["memory_metadata_unavailable"])

    async def test_memory_and_fixed_drive_capacity_skip_network_and_removable(self):
        requested = []
        def usage(root):
            requested.append(root)
            return Usage(100, 40, 60)
        system = WindowsSystem(disk_usage=usage, fixed_drive=lambda root: root == "C:\\",
                               drive_roots=lambda: ["C:\\", "D:\\", "Z:\\"],
                               memory_status=lambda: {"total_bytes": 1000, "available_bytes": 500})
        result = json.loads(await system.inspect_system())
        self.assertEqual(requested, ["C:\\"])
        self.assertEqual(result["memory"]["total_bytes"], 1000)
        self.assertEqual(len(result["fixed_local_drives"]), 1)
        self.assertNotIn("processes", result)
        self.assertFalse(result["file_contents_read"])

    async def test_async_cancellation_sets_cooperative_stop_without_replaying_observation(self):
        entered, release = threading.Event(), threading.Event()
        observed_stop = []
        calls = []
        system = WindowsSystem()
        def callback(*, budget):
            calls.append(1)
            entered.set()
            release.wait(2)
            observed_stop.append(budget.reason())
            return {}
        task = asyncio.create_task(system._call(callback))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            if observed_stop:
                break
            await asyncio.sleep(.005)
        self.assertEqual(calls, [1])
        self.assertEqual(observed_stop, ["cancelled"])

    @unittest.skipUnless(os.name == "nt", "Windows metadata adapter")
    async def test_real_owned_fixture_bytes_and_names_are_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="aura-readonly-system-") as temp:
            root = Path(temp)
            (root / "folder").mkdir()
            (root / "one.bin").write_bytes(b"content must stay unchanged")
            (root / "folder" / "two.bin").write_bytes(b"second")
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            system = WindowsSystem(categories_provider=lambda drive: [{"category": "owned_fixture", "path": str(root)}])
            result = json.loads(await system.inspect_disk_usage(root.drive))
            listing = json.loads(await system.inspect_directory(str(root)))
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(result["categories"][0]["measured_bytes"], sum(map(len, before.values())))
            self.assertEqual({entry["name"] for entry in listing["entries"]}, {"folder", "one.bin"})
            self.assertEqual(result["deleted_files"], 0)


if __name__ == "__main__":
    unittest.main()
