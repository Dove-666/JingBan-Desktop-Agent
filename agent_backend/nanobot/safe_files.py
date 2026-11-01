"""Small, explicit file tool surface; paths never leave the delegated workspace."""
from __future__ import annotations

import json
from pathlib import Path

MAX_FILE_BYTES = 1_048_576


class WorkspaceFiles:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def resolve(self, value: str) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("INVALID_PATH")
        path = Path(value)
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("OUTSIDE_WORKSPACE")
        return path

    def read(self, path: str) -> str:
        target = self.resolve(path)
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("FILE_UNAVAILABLE")
        return target.read_text(encoding="utf-8-sig")[:24000]

    def write(self, path: str, content: str, overwrite: bool = False) -> str:
        target = self.resolve(path)
        if target == self.root or not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("INVALID_WRITE")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after creating directories, including junction resolution.
        target = self.resolve(path)
        with target.open("w" if overwrite else "x", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return json.dumps({"written": str(target.relative_to(self.root)), "bytes": len(content.encode("utf-8"))})

    def search(self, query: str = "", path: str = ".") -> str:
        target = self.resolve(path)
        if not target.is_dir() or len(query) > 500:
            raise ValueError("INVALID_SEARCH")
        result = []
        count = 0
        for candidate in target.rglob("*"):
            count += 1
            if count > 2000:
                break
            # Never follow a junction/symlink outside this workspace.
            try:
                resolved = self.resolve(str(candidate))
            except ValueError:
                continue
            if not resolved.is_file():
                continue
            relative = str(candidate.relative_to(self.root))
            if not query or query.casefold() in relative.casefold():
                result.append({"path": relative})
            elif resolved.stat().st_size <= MAX_FILE_BYTES:
                try:
                    text = resolved.read_text(encoding="utf-8-sig")
                except (UnicodeError, OSError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query.casefold() in line.casefold():
                        result.append({"path": relative, "line": number, "text": line[:400]})
                        break
            if len(result) >= 100:
                break
        return json.dumps({"matches": result, "scan_limited": count > 2000}, ensure_ascii=False)
