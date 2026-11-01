"""Offline CPU adapter for Mem0 2.2.1's real FastEmbed provider.

Import only inside the isolated SDK worker. The SDK's stock constructor does
not expose FastEmbed cache/thread/offline options; its embedding implementation
is otherwise retained. Model downloads are a separate, explicit operation.
"""

from pathlib import Path

from fastembed import TextEmbedding
from mem0.embeddings.base import EmbeddingBase
from mem0.embeddings.fastembed import FastEmbedEmbedding


class LocalModelUnavailable(RuntimeError):
    """Model files are missing/corrupt; do not expose provider exception text."""


class LocalFastEmbedEmbedding(FastEmbedEmbedding):
    def __init__(self, config=None):
        EmbeddingBase.__init__(self, config)
        parameters = self.config.model_kwargs
        expected = TextEmbedding.get_embedding_size(self.config.model)
        if self.config.embedding_dims != expected:
            raise LocalModelUnavailable("Local embedding dimension differs from the selected model")
        try:
            self.dense_model = TextEmbedding(
                model_name=self.config.model,
                cache_dir=parameters["cache_dir"],
                threads=parameters["threads"],
                providers=["CPUExecutionProvider"],
                local_files_only=True,
            )
        except Exception:
            raise LocalModelUnavailable("Local embedding model is not ready") from None


def prepare_model(config: dict) -> dict:
    """Download public model artifacts only; never send user text or keys."""
    model = config["model"]
    parameters = config["model_kwargs"]
    cache = Path(parameters["cache_dir"])
    cache.mkdir(parents=True, exist_ok=True)
    try:
        embedding = TextEmbedding(
            model_name=model,
            cache_dir=str(cache),
            threads=parameters["threads"],
            providers=["CPUExecutionProvider"],
            local_files_only=False,
        )
        # Validate actual ONNX inference before reporting readiness. This fixed
        # diagnostic sentence contains no application/user/session content.
        vector = next(embedding.embed(["鲸鱼娘的本地向量模型检查。"], parallel=None))
        if len(vector) != config["embedding_dims"]:
            raise ValueError("Unexpected local vector dimensions")
    except Exception:
        raise LocalModelUnavailable("Local embedding model preparation failed") from None
    return {"ready": True, "model": model, "dimensions": len(vector), "cache_dir": str(cache)}
