"""Run the real entrypoint with temporary settings and memory-only credentials.

This helper is for the opt-in native smoke tests. It never constructs the
production SettingsManager or opens the user's Windows credential vault.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MemoryOnlyCredentials:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-dir", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args, app_args = parser.parse_known_args()
    directory = args.settings_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Do not let any optional SDK inherit production credentials or a global
    # memory data directory. No network-capable settings are configured here.
    for name in tuple(os.environ):
        if name.startswith("OPENAI_") or name in ("MEM0_API_KEY", "MEM0_DIR"):
            os.environ.pop(name, None)
    os.environ["MEM0_DIR"] = str(directory / "memory" / "sdk")
    os.environ["MEM0_TELEMETRY"] = "false"

    from PySide6.QtCore import QSettings
    from desktop_pet.settings_manager import CredentialStore, SettingsManager
    import desktop_pet.application_controller as application_module
    from desktop_pet.memory.service import MemoryService
    from desktop_pet.app import main

    backend = MemoryOnlyCredentials()

    def credential(account):
        return CredentialStore(service="AURA isolated native smoke", account=account, backend=backend)

    settings = SettingsManager(
        settings=QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat),
        credential_store=credential("chat-api-key"),
    )
    # The real controller still owns the real lazy memory service. Only its
    # credential backends are injected, exactly like other integration tests.
    application_module.MemoryService = lambda chat_settings, memory_dir: MemoryService(
        chat_settings, memory_dir,
        llm_credentials=credential("mem0-llm-key"),
        embedding_credentials=credential("mem0-embedding-key"),
    )
    args.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.state.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "pid": os.getpid(), "configuration_isolated": True,
        "credential_backend": "in_memory", "production_configuration_read": False,
        "production_credentials_read": False,
    }), encoding="utf-8")
    temporary.replace(args.state)
    return main(app_args, settings=settings)


if __name__ == "__main__":
    raise SystemExit(run())
