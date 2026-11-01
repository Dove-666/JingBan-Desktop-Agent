"""Opt-in real ONNX/Mem0 smoke; --download explicitly fetches public weights.

No production settings, credentials, messages or screenshots are used. Only
the fact-extraction LLM is a loopback fixture; embeddings and Mem0 are real.
"""

from dataclasses import replace
import argparse
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from PySide6.QtCore import QSettings

from desktop_pet.memory import MemoryService
from desktop_pet.settings_manager import ApiSettings, CredentialStore, SettingsManager
from tests.test_memory_service import LocalMemoryModels, Vault


def run(*, download=False, cache_dir=""):
    report = {"scope": "isolated-real-Mem0-and-local-ONNX; loopback-LLM-only", "checks": {}}
    with tempfile.TemporaryDirectory(prefix="aura-local-embedding-smoke-") as temporary, LocalMemoryModels() as provider:
        root = Path(temporary)
        vault = Vault()
        settings = SettingsManager(QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat), CredentialStore(backend=vault))
        settings.save_api_settings(ApiSettings(provider.base_url, "fixture-chat"))
        settings.save_api_key("fixture-chat-key")
        def create_service():
            return MemoryService(settings, root / "memory",
                                 llm_credentials=CredentialStore(account="mem0-llm-key", backend=vault),
                                 embedding_credentials=CredentialStore(account="mem0-embedding-key", backend=vault))
        service = create_service()
        try:
            service.save_settings(replace(service.load_settings(), enabled=True, automatic=True,
                                          embedding_provider="fastembed", local_embedding_cache_dir=cache_dir,
                                          relevance_threshold=0.30, timeout_seconds=30))
            if download:
                started = time.monotonic()
                report["model"] = service.prepare_local_model()
                report["download_and_load_seconds"] = round(time.monotonic() - started, 3)
            extracted = service.extract([{"role": "user", "content": "请记住，我长期喜欢大字体的桌宠评论气泡。"}],
                                        conversation_id=str(uuid4()))
            report["checks"]["real_mem0_extract_with_local_vectors"] = bool(extracted)
            service.shutdown()
            service = create_service()
            hits = service.search("评论气泡应该使用多大的字体？")
            report["checks"]["restart_semantic_retrieval"] = bool(hits)
            report["checks"]["no_remote_embedding_requests"] = all(not row["path"].endswith("/embeddings") for row in provider.requests)
            report["checks"]["real_loopback_extraction_request"] = any(row["path"].endswith("/chat/completions") for row in provider.requests)
            report["checks"]["embedding_api_key_not_saved"] = not any(
                account == "mem0-embedding-key" for _, account in vault.values
            )
            if extracted:
                service.delete(extracted[0]["id"])
            report["checks"]["actual_delete"] = service.list_memories() == []
            service.clear()
            report["checks"]["actual_clear"] = service.list_memories() == []
            report["checks"]["all"] = all(report["checks"].values())
        finally:
            service.shutdown()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="Explicitly download the public Chinese ONNX model (120 s deadline)")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--report", default="artifacts/r8_local_embedding_smoke.json")
    arguments = parser.parse_args()
    report = run(download=arguments.download, cache_dir=arguments.cache_dir)
    target = Path(arguments.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["checks"]["all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
