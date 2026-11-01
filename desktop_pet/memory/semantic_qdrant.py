"""Keep the existing semantic-only Mem0 behavior after FastEmbed installation."""

from mem0.vector_stores.qdrant import Qdrant
from qdrant_client.models import FilterSelector


class SemanticQdrant(Qdrant):
    def _get_bm25_encoder(self):
        # Mem0 2.2.1 otherwise downloads extra BM25 assets merely because the
        # optional dense-embedding package is installed. That is not part of
        # the selected local model or the user's explicit download action.
        return None

    def iter_namespace(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("A memory owner is required")
        offset = None
        query_filter = self._create_filter({"user_id": user_id})
        while True:
            rows, next_offset = self.client.scroll(
                collection_name=self.collection_name, scroll_filter=query_filter,
                limit=128, offset=offset, with_payload=True, with_vectors=False,
            )
            yield from rows
            if next_offset is None:
                return
            offset = next_offset

    def clear_namespace(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("A memory owner is required")
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=FilterSelector(filter=self._create_filter({"user_id": user_id})),
        )

    def has_records(self):
        return self.client.count(collection_name=self.collection_name, exact=True).count > 0

    def unlink_memory(self, memory_id, *, filters, expected_user_id):
        """Remove entity links locally without recomputing unchanged vectors.

        Mem0 2.2.1's generic delete helper re-embeds shared entity text. Our
        local Qdrant supports payload-only updates, so neither an embedding
        model nor a provider request is needed for this management operation.
        """
        if not expected_user_id or filters.get("user_id") != expected_user_id:
            raise ValueError("Entity cleanup requires the exact memory owner")
        scoped = {key: value for key, value in filters.items()
                  if key in ("user_id", "agent_id", "run_id") and value}
        query_filter = self._create_filter(scoped)
        offset = None
        while True:
            rows, next_offset = self.client.scroll(
                collection_name=self.collection_name, scroll_filter=query_filter,
                limit=128, offset=offset, with_payload=True, with_vectors=False,
            )
            for row in rows:
                payload = row.payload or {}
                linked = payload.get("linked_memory_ids")
                if not isinstance(linked, list) or memory_id not in linked:
                    continue
                remaining = [item for item in linked if item != memory_id]
                if remaining:
                    self.update(vector_id=row.id, vector=None,
                                payload={"linked_memory_ids": remaining})
                else:
                    self.delete(vector_id=row.id)
            if next_offset is None:
                return
            offset = next_offset
