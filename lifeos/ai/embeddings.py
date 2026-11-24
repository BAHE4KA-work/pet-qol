"""Multimodal embedding service strictly locked to gemini-embedding-2."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from typing import Any

from google import genai
from google.genai import types

from lifeos.config import Settings
from lifeos.db.models import EmbeddingRecord
from lifeos.db.repositories import LifeOSRepository, compute_sha256

ALLOWED_EMBEDDING_MODEL = "gemini-embedding-2"

_DEFAULT_MIME_BY_MODALITY: dict[str, str] = {
    "image": "image/jpeg",
    "audio": "audio/mpeg",
    "pdf": "application/pdf",
    "document": "application/pdf",
}


async def _call_sdk(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class EmbeddingService:
    """Generates and persists multimodal embeddings using gemini-embedding-2 only."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self._validate_model(settings.embedding_model)
        self.settings = settings
        self.model = ALLOWED_EMBEDDING_MODEL
        self.client = (
            client if client is not None else genai.Client(api_key=settings.gemini_api_key)
        )

    def _validate_model(self, model: str | None = None) -> str:
        target = model if model is not None else self.settings.embedding_model
        if target != ALLOWED_EMBEDDING_MODEL:
            raise ValueError(
                f"Unsupported embedding model '{target}': "
                f"only '{ALLOWED_EMBEDDING_MODEL}' is allowed"
            )
        return target

    @staticmethod
    def _extract_vectors(response: Any) -> list[list[float]]:
        embeddings = getattr(response, "embeddings", None)
        if embeddings is None and isinstance(response, dict):
            embeddings = response.get("embeddings")
        if not embeddings:
            raise ValueError("Embedding response returned no vectors")
        vectors: list[list[float]] = []
        for item in embeddings:
            values = item.get("values") if isinstance(item, dict) else getattr(item, "values", None)
            if values is None:
                raise ValueError("Embedding item missing values")
            vectors.append([float(v) for v in values])
        return vectors

    async def _embed(self, contents: Any) -> list[list[float]]:
        model = self._validate_model()
        config = types.EmbedContentConfig(output_dimensionality=self.settings.embedding_dim)
        response = await _call_sdk(
            self.client.models.embed_content,
            model=model,
            contents=contents,
            config=config,
        )
        return self._extract_vectors(response)

    async def embed_text(self, text: str) -> list[float]:
        """Embed a document or item text string."""
        vectors = await self._embed(text)
        return vectors[0]

    async def embed_query(self, text: str) -> list[float]:
        """Embed a retrieval query using the Gemini Embedding 2 task prefix."""
        formatted = f"task: search result | query: {text}"
        vectors = await self._embed(formatted)
        return vectors[0]

    async def embed_bytes(
        self,
        data: bytes,
        mime_type: str,
        *,
        caption: str | None = None,
    ) -> list[float]:
        """Embed multimodal bytes (image, audio, or PDF) via Part.from_bytes."""
        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        contents: list[Any] = [caption, part] if caption else [part]
        vectors = await self._embed(contents)
        return vectors[0]

    async def embed_batch(
        self,
        items: Sequence[str | tuple[bytes, str]],
    ) -> list[list[float]]:
        """Embed multiple text or (bytes, mime_type) items as separate vectors."""
        if not items:
            return []
        contents: list[types.Content] = []
        for item in items:
            if isinstance(item, str):
                part = types.Part.from_text(text=item)
            else:
                data, mime_type = item
                part = types.Part.from_bytes(data=data, mime_type=mime_type)
            contents.append(types.Content(parts=[part]))
        return await self._embed(contents)

    async def embed_and_store(
        self,
        repo: LifeOSRepository,
        entity_type: str,
        entity_id: str,
        text_or_bytes: str | bytes,
        modality: str = "text",
        text_preview: str = "",
        mime_type: str | None = None,
    ) -> EmbeddingRecord:
        """Compute SHA-256, skip API call if cached, otherwise embed and store."""
        content_hash = compute_sha256(text_or_bytes)
        existing = await repo.get_embedding_by_hash(content_hash)
        if existing is not None:
            return existing

        if isinstance(text_or_bytes, bytes):
            resolved_mime = mime_type or _DEFAULT_MIME_BY_MODALITY.get(
                modality, "application/octet-stream"
            )
            vector = await self.embed_bytes(text_or_bytes, resolved_mime)
            preview = text_preview[:500]
        else:
            vector = await self.embed_text(text_or_bytes)
            preview = (text_preview or text_or_bytes)[:500]

        return await repo.upsert_embedding(
            entity_type=entity_type,
            entity_id=entity_id,
            content_hash=content_hash,
            embedding=vector,
            model_name=ALLOWED_EMBEDDING_MODEL,
            modality=modality,
            text_preview=preview,
        )
