"""Read-only Windows observations for a planning agent, without GUI or a shell.

Directory walks inspect local metadata and reject observed reparse/offline
markers, including cloud placeholders. Path checks and directory opens are not
atomic against concurrent changes. They never open file contents, clean files,
or activate a program. Partial measurements are lower bounds, never freeable-byte
promises. Per-call budgets bound enumerated work; task cancellation remains
owned by the worker host even when the whole task has no duration limit.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ctypes
import json
import ntpath
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import threading
import time


_REPARSE = 0x400
_OFFLINE = 0x1000
_RECALL_ON_OPEN = 0x40000
_RECALL_ON_DATA_ACCESS = 0x400000
_UNAVAILABLE_ATTRIBUTES = _REPARSE | _OFFLINE | _RECALL_ON_OPEN | _RECALL_ON_DATA_ACCESS
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)
_WEEK = 7 * 24 * 60 * 60


class ObservationUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fixed_drive(root: str) -> bool:
    if os.name != "nt":
        return False
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint
    return function(root) == 3  # DRIVE_FIXED: no network/removable metadata scans.


def _memory_status():
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                    ("total", ctypes.c_uint64), ("available", ctypes.c_uint64),
                    ("total_page", ctypes.c_uint64), ("available_page", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                    ("available_extended_virtual", ctypes.c_uint64)]

    value = MemoryStatus()
    value.length = ctypes.sizeof(value)
    function = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    function.argtypes = [ctypes.POINTER(MemoryStatus)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(value)):
        return None
    return {"total_bytes": value.total, "available_bytes": value.available,
            "used_bytes": value.total - value.available, "load_percent": value.load}


def _drive_roots():
    if os.name != "nt":
        return []
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetLogicalDrives
    function.argtypes = []
    function.restype = ctypes.c_uint32
    mask = function()
    return [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]


def _blocked_metadata(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _UNAVAILABLE_ATTRIBUTES)


@dataclass
class _Budget:
    deadline: float
    max_entries: int
    clock: object
    cancelled: object
    entries: int = 0

    def reason(self):
        if self.cancelled():
            return "cancelled"
        if self.clock() >= self.deadline:
            return "time_limit"
        if self.entries >= self.max_entries:
            return "entry_limit"
        return None


class WindowsSystem:
    """Small asynchronous metadata tools. Native dependencies are injectable for QA."""

    def __init__(self, cancel=None, *, time_budget_seconds=6.0, max_entries=30_000,
                 clock=time.monotonic, wall_clock=time.time, disk_usage=shutil.disk_usage,
                 fixed_drive=_fixed_drive, drive_roots=_drive_roots, memory_status=_memory_status,
                 categories_provider=None, scandir=os.scandir, lstat=os.lstat):
        self.cancel = cancel or threading.Event()
        self.time_budget_seconds = min(6.0, max(.01, float(time_budget_seconds)))
        self.max_entries = min(30_000, max(1, int(max_entries)))
        self.clock, self.wall_clock = clock, wall_clock
        self.disk_usage, self.fixed_drive = disk_usage, fixed_drive
        self.drive_roots, self.memory_status = drive_roots, memory_status
        self.categories_provider = categories_provider or self._categories
        self.scandir, self.lstat = scandir, lstat

    async def _call(self, callback, **kwargs):
        stop = threading.Event()
        budget = _Budget(self.clock() + self.time_budget_seconds, self.max_entries,
                         self.clock, lambda: self.cancel.is_set() or stop.is_set())
        try:
            result = await asyncio.to_thread(callback, budget=budget, **kwargs)
            return json.dumps(result, ensure_ascii=False)
        except asyncio.CancelledError:
            stop.set()
            raise

    async def inspect_disk_usage(self, drive="", category="all"):
        return await self._call(self._inspect_disk_usage, drive=drive, category=category)

    async def inspect_directory(self, path, limit=80):
        return await self._call(self._inspect_directory, path=path, limit=limit)

    async def inspect_system(self):
        return await self._call(self._inspect_system)

    @staticmethod
    def _base():
        return {"read_only": True, "deleted_files": 0, "file_contents_read": False}

    def _drive(self, value):
        if value == "":
            value = os.environ.get("SystemDrive", "C:")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z](?::[\\/]?)?", value):
            raise ObservationUnavailable("invalid_drive")
        root = value[0].upper() + ":\\"
        if not self.fixed_drive(root):
            raise ObservationUnavailable("not_a_fixed_local_drive")
        return root

    def _path(self, value):
        if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 for c in value):
            raise ObservationUnavailable("invalid_path")
        drive, tail = ntpath.splitdrive(value)
        if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
            raise ObservationUnavailable("absolute_local_path_required")
        if ":" in tail or any(c in tail for c in "*?<>|\""):
            raise ObservationUnavailable("invalid_path")
        root = self._drive(drive)
        normalized = ntpath.normpath(root + tail.lstrip("\\/"))
        if any(_RESERVED.fullmatch(part.rstrip(" .")) for part in normalized[3:].split("\\")):
            raise ObservationUnavailable("invalid_path")
        # Never resolve: resolve() would follow the very junctions we reject.
        cursor = Path(root)
        for component in [None, *Path(normalized).parts[1:]]:
            if component is not None:
                cursor = cursor / component
            try:
                info = self.lstat(cursor)
            except FileNotFoundError:
                raise ObservationUnavailable("not_found") from None
            except PermissionError:
                raise ObservationUnavailable("permission_denied") from None
            except OSError:
                raise ObservationUnavailable("metadata_unavailable") from None
            if _blocked_metadata(info):
                raise ObservationUnavailable("reparse_or_offline_path")
        return Path(normalized)

    @staticmethod
    def _categories(root):
        windir = os.environ.get("WINDIR", root + "Windows")
        local = os.environ.get("LOCALAPPDATA", "")
        profile = os.environ.get("USERPROFILE", "")
        programdata = os.environ.get("PROGRAMDATA", root + "ProgramData")
        candidates = [
            ("user_temporary_files", ntpath.join(local, "Temp") if local else "", "review_temporary_files"),
            ("system_temporary_files", ntpath.join(windir, "Temp"), "review_temporary_files"),
            ("recycle_bin", root + "$Recycle.Bin", "review_before_emptying"),
            ("downloads", ntpath.join(profile, "Downloads") if profile else "", "personal_files_review_only"),
            ("windows_update_downloads", ntpath.join(windir, "SoftwareDistribution", "Download"), "windows_storage_settings_only"),
            ("delivery_optimization_cache", ntpath.join(programdata, "Microsoft", "Windows", "DeliveryOptimization", "Cache"), "windows_storage_settings_only"),
        ]
        return [{"category": category, "path": path, "recommendation": recommendation}
                for category, path, recommendation in candidates
                if path and ntpath.splitdrive(path)[0].casefold() == root[:2].casefold()]

    def _measure(self, path, budget, cutoff):
        measured = old_bytes = files = directories = skipped = denied = unavailable = 0
        reasons = set()
        stack = [(path, 0)]
        while stack:
            if reason := budget.reason():
                reasons.add(reason)
                break
            folder, depth = stack.pop()
            try:
                # Re-check before opening each queued directory, since its
                # metadata may have changed since its parent was enumerated.
                folder_info = self.lstat(folder)
                if _blocked_metadata(folder_info):
                    skipped += 1
                    reasons.add("reparse_or_offline_entries_skipped")
                    continue
                if not stat.S_ISDIR(folder_info.st_mode):
                    unavailable += 1
                    reasons.add("entry_changed")
                    continue
                with self.scandir(folder) as entries:
                    for entry in entries:
                        if reason := budget.reason():
                            reasons.add(reason)
                            break
                        budget.entries += 1
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if _blocked_metadata(info):
                                skipped += 1
                                reasons.add("reparse_or_offline_entries_skipped")
                            elif stat.S_ISDIR(info.st_mode):
                                directories += 1
                                if depth >= 32:
                                    reasons.add("depth_limit")
                                    skipped += 1
                                else:
                                    stack.append((Path(entry.path), depth + 1))
                            elif stat.S_ISREG(info.st_mode):
                                files += 1
                                measured += info.st_size
                                if info.st_mtime <= cutoff:
                                    old_bytes += info.st_size
                            else:
                                skipped += 1
                        except PermissionError:
                            denied += 1
                            reasons.add("permission_denied")
                        except OSError:
                            unavailable += 1
                            reasons.add("metadata_unavailable")
            except PermissionError:
                denied += 1
                reasons.add("permission_denied")
            except FileNotFoundError:
                unavailable += 1
                reasons.add("entry_disappeared")
            except OSError:
                unavailable += 1
                reasons.add("metadata_unavailable")
        return {"scan_status": "partial" if reasons else "complete", "measured_bytes": measured,
                "older_than_7_days_bytes": old_bytes, "files_observed": files,
                "directories_observed": directories, "skipped_entries": skipped,
                "denied_entries": denied, "unavailable_entries": unavailable,
                "incomplete_reasons": sorted(reasons)}

    def _inspect_disk_usage(self, drive, category="all", *, budget):
        result = {**self._base(), "used_category": None, "available_categories": []}
        try:
            root = self._drive(drive)
            definitions = list(self.categories_provider(root))
            result["available_categories"] = [item["category"] for item in definitions]
            if not isinstance(category, str) or category not in ["all", *result["available_categories"]]:
                raise ObservationUnavailable("invalid_category")
            usage = self.disk_usage(root)
        except ObservationUnavailable as error:
            return {**result, "status": "unavailable", "error": error.code, "disk": None, "categories": []}
        except OSError:
            return {**result, "status": "unavailable", "error": "disk_capacity_unavailable", "disk": None, "categories": []}
        result.update(disk={"drive": root, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
                      categories=[], reclaimable_bytes=None,
                      measurement="logical_file_sizes_lower_bound_not_disk_allocation_or_guaranteed_reclaimable_space",
                      scope="known_review_locations_only_not_full_drive_scan")
        result["used_category"] = category
        if category != "all":
            definitions = [item for item in definitions if item["category"] == category]
        cutoff = self.wall_clock() - _WEEK
        for definition in definitions:
            item = {**definition, "measured_bytes": None, "older_than_7_days_bytes": None,
                    "scan_status": "not_scanned", "reclaimable_bytes": None}
            if reason := budget.reason():
                item["incomplete_reasons"] = [reason]
            else:
                try:
                    path = self._path(definition["path"])
                    info = self.lstat(path)
                    if not stat.S_ISDIR(info.st_mode):
                        raise ObservationUnavailable("not_a_directory")
                    item.update(self._measure(path, budget, cutoff))
                    # A completely unreadable directory is unknown, not 0 bytes.
                    if item["scan_status"] == "partial" and not item["files_observed"] and not item["directories_observed"]:
                        item["measured_bytes"] = item["older_than_7_days_bytes"] = None
                except ObservationUnavailable as error:
                    item["scan_status"] = "missing" if error.code == "not_found" else "unavailable"
                    item["incomplete_reasons"] = [error.code]
                except OSError:
                    item["scan_status"] = "unavailable"
                    item["incomplete_reasons"] = ["metadata_unavailable"]
            result["categories"].append(item)
        result["status"] = "complete" if all(item["scan_status"] in {"complete", "missing"} for item in result["categories"]) else "partial"
        result["entries_examined"] = budget.entries
        result["cleanup_performed"] = False
        return result

    def _inspect_directory(self, path, limit, *, budget):
        result = {**self._base(), "entries": []}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            return {**result, "status": "unavailable", "error": "invalid_limit"}
        try:
            directory = self._path(path)
            info = self.lstat(directory)
            if _blocked_metadata(info):
                raise ObservationUnavailable("reparse_or_offline_path")
            if not stat.S_ISDIR(info.st_mode):
                raise ObservationUnavailable("not_a_directory")
            reasons = set()
            with self.scandir(directory) as entries:
                for entry in entries:
                    if reason := budget.reason():
                        reasons.add(reason)
                        break
                    if len(result["entries"]) >= limit:
                        reasons.add("result_limit")
                        break
                    budget.entries += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                        blocked = _blocked_metadata(info)
                        kind = "unavailable" if blocked else "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
                        result["entries"].append({"name": entry.name, "kind": kind,
                                                  "logical_bytes": info.st_size if kind == "file" else None,
                                                  "metadata_status": "reparse_or_offline_skipped" if blocked else "observed"})
                    except OSError:
                        reasons.add("metadata_unavailable")
            result["entries"].sort(key=lambda item: item["name"].casefold())
            result.update(status="partial" if reasons else "complete", path=str(directory),
                          incomplete_reasons=sorted(reasons), recursive=False)
            return result
        except ObservationUnavailable as error:
            return {**result, "status": "unavailable", "error": error.code}
        except PermissionError:
            return {**result, "status": "unavailable", "error": "permission_denied"}
        except OSError:
            return {**result, "status": "unavailable", "error": "metadata_unavailable"}

    def _inspect_system(self, *, budget):
        try:
            memory = self.memory_status()
        except OSError:
            memory = None
        result = {**self._base(), "status": "complete", "os": platform.system(),
                  "release": platform.release(), "architecture": platform.machine(),
                  "memory": memory, "fixed_local_drives": []}
        if memory is None:
            result.update(status="partial", incomplete_reasons=["memory_metadata_unavailable"])
        for root in self.drive_roots():
            if reason := budget.reason():
                result["status"] = "partial"
                result.setdefault("incomplete_reasons", []).append(reason)
                break
            if self.fixed_drive(root):
                try:
                    usage = self.disk_usage(root)
                    result["fixed_local_drives"].append({"drive": root, "total_bytes": usage.total,
                                                         "used_bytes": usage.used, "free_bytes": usage.free})
                except OSError:
                    result["fixed_local_drives"].append({"drive": root, "status": "unavailable"})
                    result["status"] = "partial"
        return result
