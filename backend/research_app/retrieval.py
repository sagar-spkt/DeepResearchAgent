"""Owner-scoped hybrid retrieval; local fixtures never download embedding weights."""

import hashlib
import math
import os
import re
from functools import lru_cache

from sqlalchemy import select, text


def terms(value):
    return re.findall(r"\w+", value.lower())


@lru_cache(maxsize=2)
def embedding_model(name):
    from sentence_transformers import SentenceTransformer

    # Prepare weights during a controlled image build; requests cannot trigger downloads.
    return SentenceTransformer(
        name,
        device="cpu",
        local_files_only=True,
        cache_folder=os.environ.get("SENTENCE_TRANSFORMERS_HOME"),
    )


def embed_texts(texts, settings):
    if settings.embedding_provider == "sentence-transformers":
        return (
            embedding_model(settings.embedding_model)
            .encode(texts, normalize_embeddings=True)
            .tolist()
        )
    if settings.environment != "local":
        raise ValueError("Fake embeddings are restricted to local mode")
    vectors = []
    for value in texts:
        vector = [0.0] * 384
        for token in terms(value):
            vector[int.from_bytes(hashlib.sha256(token.encode()).digest()[:4]) % 384] += 1
        norm = math.sqrt(sum(x * x for x in vector)) or 1
        vectors.append([x / norm for x in vector])
    return vectors


def retrieve(factory, settings, user_id, conversation_id, query, limit=5):
    from .models import Attachment, Chunk, Document

    query_vector = embed_texts([query], settings)[0]
    with factory() as session:
        # Every tool call reloads attachments/status so mid-run uploads and deletion take effect.
        if session.bind.dialect.name == "postgresql":
            # The explicit migration creates a vector column/index. No unscoped vector search.
            rows = (
                session.execute(
                    text("""
                SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id
                JOIN attachments a ON a.document_id=d.id
                WHERE c.user_id=:user AND d.user_id=:user AND a.user_id=:user
                AND a.conversation_id=:conversation AND d.status='ready'
                ORDER BY (c.embedding_vector <=> CAST(:vector AS vector))
                - 0.1 * ts_rank_cd(to_tsvector('english', c.text), plainto_tsquery('english', :query))
                LIMIT :limit
            """),
                    {
                        "user": user_id,
                        "conversation": conversation_id,
                        "vector": str(query_vector),
                        "query": query,
                        "limit": limit,
                    },
                )
                .scalars()
                .all()
            )
            chunks = list(session.scalars(select(Chunk).where(Chunk.id.in_(rows))))
            positions = {key: i for i, key in enumerate(rows)}
            chunks.sort(key=lambda c: positions[c.id])
        else:
            chunks = list(
                session.scalars(
                    select(Chunk)
                    .join(Document, Document.id == Chunk.document_id)
                    .join(Attachment, Attachment.document_id == Document.id)
                    .where(
                        Chunk.user_id == user_id,
                        Document.user_id == user_id,
                        Attachment.user_id == user_id,
                        Attachment.conversation_id == conversation_id,
                        Document.status == "ready",
                    )
                )
            )
            query_terms = set(terms(query))

            def score(chunk):
                vector_score = sum(a * b for a, b in zip(query_vector, chunk.embedding))
                return vector_score + len(query_terms & set(terms(chunk.text))) / max(
                    len(query_terms), 1
                )

            chunks.sort(key=score, reverse=True)
            chunks = chunks[:limit]
        result = []
        for chunk in chunks:
            document = session.get(Document, chunk.document_id)
            result.append(
                {
                    "document_id": document.id,
                    "name": document.name,
                    "locator": chunk.locator,
                    "text": chunk.text[:4000],
                }
            )
        return result
