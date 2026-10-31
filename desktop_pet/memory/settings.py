"""Non-secret preferences for the local Mem0 store and its model providers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from uuid import UUID

from desktop_pet.settings_manager import ApiSettings, SettingsError


LOCAL_EMBEDDING_MODELS = {
    "BAAI/bge-small-zh-v1.5": 512,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = False
    automatic: bool = False
    user_id: str = ""
    use_chat_model: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_provider: str = "openai"
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    local_embedding_cache_dir: str = ""
    local_embedding_threads: int = 2
    timeout_seconds: float = 20.0
    relevance_threshold: float = 0.60
    max_results: int = 4
    extraction_context_tokens: int = 65536
    extraction_output_tokens: int = 1024

    @property
    def effective_embedding_dimensions(self) -> int:
        """Local model dimensions are a verified catalog value, not a guess."""
        if self.embedding_provider == "fastembed":
            return LOCAL_EMBEDDING_MODELS[self.local_embedding_model]
        return self.embedding_dimensions

    def validate(self) -> None:
        if any(type(value) is not bool for value in (self.enabled, self.automatic, self.use_chat_model)):
            raise SettingsError("记忆开关必须为布尔值。")
        try:
            if str(UUID(self.user_id)) != self.user_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SettingsError("记忆用户标识无效。") from None
        ApiSettings(self.llm_base_url, self.llm_model, self.timeout_seconds).validate(require_model=False)
        if self.embedding_provider not in ("openai", "fastembed"):
            raise SettingsError("Embedding 提供方必须为 openai 或 fastembed。")
        if self.embedding_provider == "openai":
            ApiSettings(self.embedding_base_url, self.embedding_model, self.timeout_seconds).validate(require_model=False)
        if not isinstance(self.local_embedding_model, str) or self.local_embedding_model not in LOCAL_EMBEDDING_MODELS:
            raise SettingsError("请选择列表中支持的本地 Embedding 模型。")
        if type(self.local_embedding_threads) is not int or not 1 <= self.local_embedding_threads <= 8:
            raise SettingsError("本地 Embedding CPU 线程必须为 1～8。")
        if (not isinstance(self.local_embedding_cache_dir, str)
                or len(self.local_embedding_cache_dir) > 2048
                or any(ord(char) < 32 for char in self.local_embedding_cache_dir)):
            raise SettingsError("本地 Embedding 缓存目录无效。")
        if self.local_embedding_cache_dir and not Path(self.local_embedding_cache_dir).expanduser().is_absolute():
            raise SettingsError("本地 Embedding 缓存目录必须为绝对路径，或留空使用默认缓存。")
        if type(self.embedding_dimensions) is not int or not 1 <= self.embedding_dimensions <= 65536:
            raise SettingsError("Embedding 维度必须为 1～65536 的整数，并与模型实际输出一致。")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 8:
            raise SettingsError("记忆检索数量必须为 1～8。")
        if (type(self.extraction_context_tokens) is not int
                or not 4096 <= self.extraction_context_tokens <= 262144):
            raise SettingsError("Mem0 提炼总预算必须为 4096～262144 Token。")
        if (type(self.extraction_output_tokens) is not int
                or not 32 <= self.extraction_output_tokens <= 1024
                or self.extraction_output_tokens >= self.extraction_context_tokens):
            raise SettingsError("Mem0 提炼输出预留必须为 32～1024 Token，且小于总预算。")
        if (
            isinstance(self.relevance_threshold, bool)
            or not isinstance(self.relevance_threshold, (float, int))
            or not math.isfinite(self.relevance_threshold)
            or not 0.0 <= self.relevance_threshold <= 1.0
        ):
            raise SettingsError("记忆相关度阈值必须为 0～1。")
