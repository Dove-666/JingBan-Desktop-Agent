"""Private DSH homes and native session IDs, separate from legacy transcripts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .identities import conversation_uuid

_SCOPE = re.compile(r"[a-f0-9]{64}\Z")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


def session_scope(settings, api, *, generation="", key=""):
    """Credential changes separate sessions without storing the credential."""
    value = {"version": 2, "generation": generation,
             "api": {name: api.get(name) for name in ("base_url", "model")},
             # Native resume retains the previous effort when a new runtime
             # omits it. Reset/omit must not silently inherit an older choice.
             "reasoning": api.get("reasoning"),
             "credential": hashlib.sha256(key.encode("utf-8")).hexdigest(),
             "tools": settings.tools_enabled, "computer": settings.computer_enabled,
             "workspace": str(Path(settings.workspace).resolve())}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def valid_session_id(value):
    return isinstance(value, str) and _SESSION.fullmatch(value) is not None


class DshSessionStore:
    """Only the official runner reads/writes DSH's native session format."""
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _directory(self, conversation_id, scope=None):
        conversation_uuid(conversation_id)
        if scope is not None and (not isinstance(scope, str) or not _SCOPE.fullmatch(scope)):
            raise ValueError("INVALID_DSH_SCOPE")
        target = self.root / conversation_id
        if scope is not None:
            target /= scope
        resolved = target.resolve()
        if (not resolved.is_relative_to(self.root) or resolved == self.root
                or resolved != target.absolute() or target.is_symlink()):
            raise ValueError("INVALID_DSH_SESSION_DIRECTORY")
        return resolved

    def prepare(self, conversation_id, scope):
        directory = self._directory(conversation_id, scope)
        directory.mkdir(parents=True, exist_ok=True)
        metadata = directory / "session.json"
        home = directory / "home"
        if metadata.is_symlink() or metadata.resolve() != metadata.absolute() or home.resolve() != home.absolute():
            raise ValueError("INVALID_DSH_SESSION_DIRECTORY")
        session_id = None
        if metadata.exists():
            if metadata.stat().st_size > 4096:
                raise ValueError("INVALID_DSH_SESSION_METADATA")
            value = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"version", "dsh_session_id"} or value["version"] != 1:
                raise ValueError("INVALID_DSH_SESSION_METADATA")
            session_id = value["dsh_session_id"]
            if not valid_session_id(session_id):
                raise ValueError("INVALID_DSH_SESSION_ID")
        return {"home": str(home), "session_id": session_id}

    def save(self, conversation_id, scope, session_id):
        if not valid_session_id(session_id):
            raise ValueError("INVALID_DSH_SESSION_ID")
        directory = self._directory(conversation_id, scope)
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "dsh_session_id": session_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "session.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def clear(self, conversation_id=None):
        if not self.root.exists():
            return
        if conversation_id is not None:
            targets = [self._directory(conversation_id)]
        else:
            targets = [self._directory(item.name) for item in self.root.iterdir() if item.is_dir()]
        for target in targets:
            # Resolve and constrain every recursive deletion before touching it.
            resolved = target.resolve()
            if resolved == self.root or not resolved.is_relative_to(self.root) or target.is_symlink():
                raise ValueError("INVALID_DSH_SESSION_DIRECTORY")
            if target.exists():
                shutil.rmtree(target)
