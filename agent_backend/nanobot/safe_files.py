"""Versioned, bounded file tools over host-owned workspace capabilities."""
from __future__ import annotations

import hashlib
from itertools import islice
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading

MAX_FILE_BYTES = 1_048_576
MAX_DOCUMENT_BYTES = 16 * MAX_FILE_BYTES
MAX_PAGE_CHARACTERS = 24_000
MAX_SEARCH_ENTRIES = 2000
MAX_SEARCH_READ_BYTES = 32 * MAX_FILE_BYTES
MAX_SEARCH_RESPONSE_BYTES = 65_536
MAX_SEARCH_LINE_MATCHES = 20
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")


class WorkspaceFiles:
    def __init__(self, root: Path, *, grants=(), read_roots=(), write_roots=()):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("INVALID_WORKSPACE")
        self._read_roots = []
        self._write_roots = []
        self._observed = {}
        self._lock = threading.RLock()
        if not isinstance(grants, (list, tuple)) or len(grants) > 32:
            raise ValueError("INVALID_FILE_GRANTS")
        for grant in grants:
            self.add_grant(grant)
        for access, roots in (("read", read_roots), ("write", write_roots)):
            if not isinstance(roots, (list, tuple)) or len(roots) > 32:
                raise ValueError("INVALID_FILE_GRANTS")
            for path in roots:
                path = Path(path)
                self.add_grant({"path": str(path), "access": access, "directory": path.is_dir()})

    def add_grant(self, grant):
        """Accept only host-confirmed grants; this is never a model-facing tool."""
        if (not isinstance(grant, dict) or set(grant) != {"path", "access", "directory"}
                or not isinstance(grant["path"], str) or type(grant["directory"]) is not bool
                or not isinstance(grant["access"], str)
                or grant["access"] not in {"read", "write"}):
            raise ValueError("INVALID_FILE_GRANTS")
        path = Path(grant["path"])
        if not path.is_absolute() or "\0" in str(path):
            raise ValueError("INVALID_FILE_GRANTS")
        canonical = path.resolve()
        if grant["directory"]:
            if not canonical.is_dir():
                raise ValueError("FILE_GRANT_UNAVAILABLE")
        elif canonical.is_dir() or (grant["access"] == "read" and not canonical.is_file()):
            raise ValueError("FILE_GRANT_UNAVAILABLE")
        elif not canonical.exists() and not canonical.parent.is_dir():
            raise ValueError("FILE_GRANT_UNAVAILABLE")
        roots = self._write_roots if grant["access"] == "write" else self._read_roots
        item = canonical, grant["directory"]
        with self._lock:
            if len(self._read_roots) + len(self._write_roots) >= 64 and item not in roots:
                raise ValueError("FILE_GRANT_LIMIT")
            if item not in roots:
                roots.append(item)
        return {"path": str(canonical), "access": grant["access"], "directory": grant["directory"]}

    def resolve(self, value: str, *, write=False) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
            raise ValueError("INVALID_PATH")
        path = Path(value)
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if path.is_relative_to(self.root):
            return path
        grants = self._write_roots if write else (*self._read_roots, *self._write_roots)
        if not any(path == grant or is_dir and path.is_relative_to(grant) for grant, is_dir in grants):
            raise ValueError("OUTSIDE_WORKSPACE")
        return path

    def display_path(self, target: Path) -> str:
        try:
            return target.relative_to(self.root).as_posix()
        except ValueError:
            return str(target)

    def _snapshot(self, path, *, maximum=MAX_FILE_BYTES, observe=False):
        target = self.resolve(path)
        try:
            before = target.stat()
            if not target.is_file() or before.st_size > maximum:
                raise ValueError("FILE_UNAVAILABLE")
            with target.open("rb") as stream:
                raw = stream.read(maximum + 1)
            after = target.stat()
            identity = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino)
            if len(raw) > maximum or identity(before) != identity(after) or self.resolve(path) != target:
                raise ValueError("FILE_CHANGED")
        except OSError:
            raise ValueError("FILE_UNAVAILABLE") from None
        digest = hashlib.sha256(raw).hexdigest()
        if observe:
            self._observed[target] = digest
        return target, raw, digest

    def read_bytes(self, path, *, maximum=MAX_DOCUMENT_BYTES):
        """Trusted parser entry; records the observed version without decoding."""
        with self._lock:
            return self._snapshot(path, maximum=maximum, observe=True)

    @staticmethod
    def _expected(value):
        if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
            raise ValueError("INVALID_EXPECTED_HASH")
        return value.lower() if value is not None else None

    def read_page(self, path: str, offset: int = 0, limit: int = 12_000,
                  unit: str = "characters", expected_hash: str | None = None) -> str:
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= MAX_PAGE_CHARACTERS or not isinstance(unit, str)
                or unit not in {"characters", "lines"}):
            raise ValueError("INVALID_READ_RANGE")
        expected = self._expected(expected_hash)
        with self._lock:
            target, raw, digest = self._snapshot(path, observe=False)
            if expected is not None and digest != expected:
                raise ValueError("FILE_CHANGED")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise ValueError("UNSUPPORTED_TEXT_ENCODING") from None
            self._observed[target] = digest
            lines = text.splitlines(keepends=True)
            if unit == "lines":
                selected = lines[offset:offset + limit]
                body = "".join(selected)
                if len(body) > MAX_PAGE_CHARACTERS:
                    raise ValueError("LINE_RANGE_TOO_LARGE_USE_CHARACTERS")
                total, returned = len(lines), len(selected)
            else:
                body = text[offset:offset + limit]
                total, returned = len(text), len(body)
            end = min(total, offset + returned)
            return json.dumps({"path": self.display_path(target), "encoding": "utf-8",
                "sha256": digest, "bytes": len(raw), "total_characters": len(text),
                "total_lines": len(lines), "unit": unit, "offset": offset,
                "returned": returned, "truncated": end < total,
                "next_offset": end if end < total else None, "text": body}, ensure_ascii=False)

    def read(self, path: str, offset=None, limit=None, unit="characters", expected_hash=None) -> str:
        """Legacy short reads stay plain text; long reads explicitly paginate."""
        if offset is not None or limit is not None or expected_hash is not None or unit != "characters":
            return self.read_page(path, 0 if offset is None else offset,
                                  12_000 if limit is None else limit, unit, expected_hash)
        with self._lock:
            _, raw, _ = self._snapshot(path, observe=True)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise ValueError("UNSUPPORTED_TEXT_ENCODING") from None
            return text if len(text) <= MAX_PAGE_CHARACTERS else self.read_page(path, limit=MAX_PAGE_CHARACTERS)

    def _version_for_write(self, target, overwrite, expected_hash):
        expected = self._expected(expected_hash)
        if target.exists():
            if not overwrite:
                raise FileExistsError("FILE_EXISTS")
            if not target.is_file():
                raise ValueError("INVALID_WRITE")
            expected = expected or self._observed.get(target)
            if expected is None:
                raise ValueError("READ_BEFORE_OVERWRITE")
            _, _, actual = self._snapshot(str(target), maximum=MAX_DOCUMENT_BYTES)
            if actual != expected:
                raise ValueError("FILE_CHANGED")
            return expected
        if expected is not None:
            raise ValueError("FILE_CHANGED")
        return None

    def write_bytes(self, path: str, content: bytes, overwrite=False, expected_hash=None) -> str:
        if type(overwrite) is not bool or not isinstance(content, bytes) or len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("INVALID_WRITE")
        with self._lock:
            target = self.resolve(path, write=True)
            if target == self.root:
                raise ValueError("INVALID_WRITE")
            expected = self._version_for_write(target, overwrite, expected_hash)
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.resolve(path, write=True) != target:
                raise ValueError("FILE_CHANGED")
            descriptor, temporary = tempfile.mkstemp(prefix=".aura-write-", suffix=".tmp", dir=target.parent)
            temporary = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Optimistic version check before an atomic namespace update.
                # This is not a lock against hostile, uncooperative host software.
                checked = self._version_for_write(target, overwrite, expected)
                if checked is None:
                    if os.name == "nt":
                        # Windows rename is atomic and refuses an existing
                        # target; unlike hardlinks it also works on FAT/SMB.
                        os.rename(temporary, target)
                    else:
                        os.link(temporary, target)
                        temporary.unlink()
                else:
                    os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            digest = hashlib.sha256(content).hexdigest()
            self._observed[target] = digest
            artifact = {"path": self.display_path(target), "name": target.name,
                        "sha256": digest, "bytes": len(content),
                        "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream"}
            return json.dumps({"written": artifact["path"], "bytes": len(content),
                               "sha256": digest, "artifact": artifact}, ensure_ascii=False)

    def write(self, path: str, content: str, overwrite: bool = False, expected_hash=None) -> str:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("INVALID_WRITE")
        return self.write_bytes(path, content.encode("utf-8"), overwrite, expected_hash)

    def edit(self, path: str, old_text: str, new_text: str, expected_hash=None,
             replace_all: bool = False) -> str:
        if (not isinstance(old_text, str) or not old_text or not isinstance(new_text, str)
                or type(replace_all) is not bool):
            raise ValueError("INVALID_EDIT")
        with self._lock:
            target = self.resolve(path, write=True)
            expected = self._version_for_write(target, True, expected_hash)
            _, raw, actual = self._snapshot(path)
            if actual != expected:
                raise ValueError("FILE_CHANGED")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise ValueError("UNSUPPORTED_TEXT_ENCODING") from None
            count = text.count(old_text)
            if not count:
                raise ValueError("EDIT_TEXT_NOT_FOUND")
            if count != 1 and not replace_all:
                raise ValueError("EDIT_TEXT_AMBIGUOUS")
            result = text.replace(old_text, new_text, -1 if replace_all else 1)
            if raw.startswith(b"\xef\xbb\xbf"):
                result = "\ufeff" + result
            return self.write(path, result, overwrite=True, expected_hash=expected)

    @staticmethod
    def _search_excerpt(number, line, folded_query):
        """Keep source character offsets even when casefold expands a character."""
        folded_line = line.casefold()
        index = folded_line.find(folded_query)
        if index < 0:
            return None
        match_end = index + len(folded_query)
        if len(folded_line) != len(line):
            folded, original_start = 0, None
            for position, char in enumerate(line):
                following = folded + len(char.casefold())
                if original_start is None and folded <= index < following:
                    original_start = position
                if folded < match_end <= following:
                    index, match_end = original_start, position + 1
                    break
                folded = following
        start = max(0, index - 160)
        end = min(len(line), max(start + 400, match_end))
        return {"line": number, "character_offset": start, "text": line[start:end],
                "truncated": start > 0 or end < len(line)}

    def search(self, query: str = "", path: str = ".", offset: int = 0, limit: int = 100,
               line_offset: int = 0, expected_hash: str | None = None) -> str:
        target = self.resolve(path)
        is_file = target.is_file()
        if (not (is_file or target.is_dir()) or not isinstance(query, str) or len(query) > 500
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
                or type(line_offset) is not int or line_offset < 0
                or (not is_file and line_offset != 0)):
            raise ValueError("INVALID_SEARCH")
        expected = self._expected(expected_hash)
        if expected is not None and not is_file:
            raise ValueError("EXPECTED_HASH_REQUIRES_FILE")
        folded_query = query.casefold()
        result, seen = [], set()
        scanned = matching_files = read_bytes = 0
        scan_limited = read_limited = response_limited = more_files = False
        skipped = {}

        def payload(items):
            return {"matches": items, "offset": offset,
                    # Legacy offset always counts FILE matches, not line hits.
                    "next_offset": offset + len(items) if more_files else None,
                    "scan_limited": scan_limited, "read_limited": read_limited,
                    "response_limited": response_limited, "entries_scanned": scanned,
                    "content_bytes_read": read_bytes, "skipped_contents": skipped,
                    "truncated": (more_files or scan_limited or read_limited or bool(skipped)
                                  or any(item.get("line_matches_truncated", False) for item in items))}

        def serialized(items):
            return json.dumps(payload(items), ensure_ascii=False)

        # Reserve envelope room because flags/counters can grow after a hit.
        item_budget = MAX_SEARCH_RESPONSE_BYTES - 2048
        with self._lock:
            candidates = (target,) if is_file else target.rglob("*")
            for candidate in islice(candidates, MAX_SEARCH_ENTRIES):
                scanned += 1
                try:
                    resolved = self.resolve(str(candidate))
                    if not resolved.is_file() or resolved in seen:
                        continue
                    seen.add(resolved)
                    relative = self.display_path(resolved)
                    filename_match = not query or folded_query in relative.casefold()
                    item = {"path": relative} if filename_match else None
                    if query or expected is not None:
                        size = resolved.stat().st_size
                        remaining = MAX_SEARCH_READ_BYTES - read_bytes
                        if size > MAX_FILE_BYTES:
                            if expected is not None:
                                raise ValueError("FILE_UNAVAILABLE")
                            skipped["FILE_TOO_LARGE"] = skipped.get("FILE_TOO_LARGE", 0) + 1
                        elif size >= remaining:
                            # The snapshot's one-byte overflow probe is also in
                            # the IO budget, even if the file grows mid-read.
                            read_limited = True
                            break
                        else:
                            maximum = min(MAX_FILE_BYTES, remaining - 1)
                            try:
                                actual, raw, digest = self._snapshot(str(candidate), maximum=maximum)
                            except ValueError as exc:
                                # A failed snapshot might have read maximum+1
                                # bytes. Charge conservatively before rejecting.
                                read_bytes += maximum + 1
                                if str(exc) == "FILE_CHANGED" or is_file:
                                    raise
                                skipped["FILE_UNAVAILABLE"] = skipped.get("FILE_UNAVAILABLE", 0) + 1
                                continue
                            read_bytes += len(raw)
                            if expected is not None and digest != expected:
                                raise ValueError("FILE_CHANGED")
                            relative = self.display_path(actual)
                            filename_match = not query or folded_query in relative.casefold()
                            item = {"path": relative, "sha256": digest, "bytes": len(raw)}
                            line_matches, matching_lines = [], 0
                            try:
                                text = raw.decode("utf-8-sig")
                            except UnicodeError:
                                skipped["UNSUPPORTED_TEXT_ENCODING"] = skipped.get("UNSUPPORTED_TEXT_ENCODING", 0) + 1
                                if not filename_match:
                                    continue
                                item["content_status"] = "unsupported_encoding"
                            else:
                                if query:
                                    for number, line in enumerate(text.splitlines(), 1):
                                        if number <= line_offset:
                                            continue
                                        hit = self._search_excerpt(number, line, folded_query)
                                        if hit is not None:
                                            matching_lines += 1
                                            if len(line_matches) < MAX_SEARCH_LINE_MATCHES:
                                                line_matches.append(hit)
                                if not filename_match and not line_matches:
                                    continue
                                if line_matches:
                                    # Preserve the original first-hit fields.
                                    item.update(line_matches[0])
                                item.update(line_matches=line_matches,
                                            line_offset=line_offset,
                                            line_matches_total=matching_lines,
                                            line_matches_truncated=matching_lines > len(line_matches),
                                            next_line_offset=(line_matches[-1]["line"]
                                                              if matching_lines > len(line_matches) else None))
                    if item is None:
                        continue
                except ValueError as exc:
                    if str(exc) == "FILE_CHANGED" or is_file:
                        raise
                    continue
                except (OSError, UnicodeError):
                    if is_file:
                        raise ValueError("FILE_UNAVAILABLE") from None
                    skipped["FILE_UNAVAILABLE"] = skipped.get("FILE_UNAVAILABLE", 0) + 1
                    continue
                matching_files += 1
                if matching_files <= offset:
                    continue
                if len(result) >= limit:
                    more_files = True
                    break
                # All text is untrusted; bound its encoded JSON size, including
                # quotes, control-character escapes and multibyte UTF-8.
                if not result:
                    while len(serialized([item]).encode("utf-8")) > item_budget:
                        hits = item.get("line_matches", [])
                        if len(hits) <= 1:
                            raise ValueError("SEARCH_RESULT_TOO_LARGE")
                        hits.pop()
                        item["line_matches_truncated"] = True
                        item["next_line_offset"] = hits[-1]["line"]
                if len(serialized([*result, item]).encode("utf-8")) > item_budget:
                    more_files = response_limited = True
                    break
                result.append(item)
            if not is_file and scanned >= MAX_SEARCH_ENTRIES:
                # Do not consume a 2001st entry merely to discover truncation.
                # At the cap, completeness is conservatively unknown.
                scan_limited = True
        output = serialized(result)
        if len(output.encode("utf-8")) > MAX_SEARCH_RESPONSE_BYTES:
            raise ValueError("SEARCH_RESULT_TOO_LARGE")
        return output
