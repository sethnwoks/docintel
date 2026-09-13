import asyncio
import json
import logging
import os
from typing import Any

import openai
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.async_session import get_async_db
from app.api.dependencies import get_current_tenant_id, get_current_user_id
from app.services.reranker import score_pairs, RERANKER_MODEL_FILE

router = APIRouter(prefix="/query", tags=["query"])
logger = logging.getLogger(__name__)

OPENAI_MODEL = "gpt-4o-mini"
HYBRID_LIMIT = 50
FINAL_LIMIT = 7
BM25_WEIGHT = 0.4
VECTOR_WEIGHT = 0.6
RERANK_CANDIDATES = 20
RERANK_BATCH_SIZE = 8

openai_client: openai.AsyncOpenAI | None = None


def get_openai_client():
    global openai_client
    if openai_client is None:
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
        openai_client = openai.AsyncOpenAI(api_key=openai_key)
    return openai_client


class QueryRequest(BaseModel):
    question: str
    document_ids: list[str] | None = None


class SourceReference(BaseModel):
    filename: str
    chunk_index: int
    document_id: str
    similarity: float
    bm25: bool
    reranked: bool = False


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceReference]


def _build_where_clause(document_ids: list[str] | None) -> tuple[str, dict]:
    if document_ids:
        return (
            "AND document_id = ANY(:document_ids)",
            {"document_ids": document_ids},
        )
    return ("", {})


def _score_normalization_merge(
    bm25_rows: list[tuple[str, str, Any, float]],
    vector_rows: list[tuple[str, str, Any, float]],
    bm25_weight: float = BM25_WEIGHT,
    vector_weight: float = VECTOR_WEIGHT,
    limit: int = FINAL_LIMIT,
) -> tuple[list[dict], int]:
    scores: dict[str, dict] = {}

    bm25_scores = [float(r[3] or 0.0) for r in bm25_rows]
    bm25_min = min(bm25_scores) if bm25_scores else 0.0
    bm25_max = max(bm25_scores) if bm25_scores else 0.0
    bm25_range = bm25_max - bm25_min if bm25_max > bm25_min else 1.0

    for rank, (chunk_id, content, metadata, bm25_score) in enumerate(bm25_rows, start=1):
        norm_score = (float(bm25_score or 0.0) - bm25_min) / bm25_range
        scores.setdefault(chunk_id, {
            "id": chunk_id,
            "content": content,
            "metadata": metadata,
            "combined_score": 0.0,
            "bm25_norm": 0.0,
            "vector_norm": 0.0,
            "bm25": False,
            "vector": False,
        })
        scores[chunk_id]["bm25_norm"] = norm_score
        scores[chunk_id]["combined_score"] += bm25_weight * norm_score
        scores[chunk_id]["bm25"] = True

    vector_scores = [float(r[3] or 0.0) for r in vector_rows]
    vec_min = min(vector_scores) if vector_scores else 0.0
    vec_max = max(vector_scores) if vector_scores else 0.0
    vec_range = vec_max - vec_min if vec_max > vec_min else 1.0

    for rank, (chunk_id, content, metadata, distance) in enumerate(vector_rows, start=1):
        norm_distance = (float(distance or 0.0) - vec_min) / vec_range
        norm_similarity = 1.0 - norm_distance
        entry = scores.get(chunk_id)
        if entry is None:
            entry = {
                "id": chunk_id,
                "content": content,
                "metadata": metadata,
                "combined_score": 0.0,
                "bm25_norm": 0.0,
                "vector_norm": 0.0,
                "bm25": False,
                "vector": False,
            }
            scores[chunk_id] = entry
        entry["vector_norm"] = norm_similarity
        entry["combined_score"] += vector_weight * norm_similarity
        entry["vector"] = True

    ranked = sorted(scores.values(), key=lambda x: x["combined_score"], reverse=True)[:limit]
    bm25_in_final = sum(1 for r in ranked if r["bm25"])
    return ranked, bm25_in_final


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    if not set_a and not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _deduplicate_chunks(
    chunks: list[dict],
    threshold: float = 0.7,
) -> list[dict]:
    """Remove chunks with high Jaccard word overlap, keeping higher-scored ones first."""
    tokenized: list[tuple[dict, set[str]]] = []
    for chunk in chunks:
        words = set(chunk.get("content", "").lower().split())
        tokenized.append((chunk, words))

    kept: list[tuple[dict, set[str]]] = []
    for chunk, words in tokenized:
        if any(_jaccard_similarity(words, k_words) > threshold for _, k_words in kept):
            continue
        kept.append((chunk, words))

    return [chunk for chunk, _ in kept]


@router.post("/", response_model=QueryResponse)
async def query_rag(
    request: QueryRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(get_current_tenant_id),
    user_id: str = Depends(get_current_user_id),
):
    try:
        openai_client = get_openai_client()
        response = await openai_client.embeddings.create(
            input=[request.question], model="text-embedding-3-small"
        )
        question_embedding = response.data[0].embedding

        where_clause, extra_params = _build_where_clause(request.document_ids)
        base_params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "embedding": str(question_embedding),
            **extra_params,
        }

        bm25_sql = text(f"""
            SELECT id, content, metadata, pdb.score(id) AS bm25_score
            FROM chunks
            WHERE content ||| :question
              AND tenant_id = :tenant_id
              {where_clause}
            ORDER BY bm25_score DESC
            LIMIT :hybrid_limit
        """)
        bm25_params = {**base_params, "question": request.question, "hybrid_limit": HYBRID_LIMIT}
        bm25_result = await db.execute(bm25_sql, bm25_params)
        bm25_rows = bm25_result.fetchall()
        logger.info(
            "[BM25] question=%r tenant=%s hits=%d",
            request.question[:100], tenant_id, len(bm25_rows),
        )

        vector_sql = text(f"""
            SELECT id, content, metadata, embedding <-> :embedding AS distance
            FROM chunks
            WHERE tenant_id = :tenant_id
              {where_clause}
            ORDER BY embedding <-> :embedding
            LIMIT :hybrid_limit
        """)
        vector_params = {**base_params, "hybrid_limit": HYBRID_LIMIT}
        vector_result = await db.execute(vector_sql, vector_params)
        vector_rows = vector_result.fetchall()
        logger.info(
            "[VECTOR] question=%r tenant=%s hits=%d",
            request.question[:100], tenant_id, len(vector_rows),
        )

        merged, bm25_in_final = _score_normalization_merge(
            [(str(r[0]), r[1], r[2], float(r[3] or 0.0)) for r in bm25_rows],
            [(str(r[0]), r[1], r[2], float(r[3] or 0.0)) for r in vector_rows],
        )
        logger.info(
            "[HYBRID] question=%r tenant=%s bm25_in_final=%d/%d",
            request.question[:100], tenant_id, bm25_in_final, FINAL_LIMIT,
        )

        if not merged:
            answer = "I don't have enough information to answer that question. Please upload relevant documents first."
            sources = []
        else:
            candidates = merged[:RERANK_CANDIDATES]
            before_dedup = len(candidates)
            candidates = _deduplicate_chunks(candidates)
            after_dedup = len(candidates)
            logger.info(
                "[DEDUP] question=%r tenant=%s before=%d after=%d",
                request.question[:100], tenant_id, before_dedup, after_dedup,
            )
            passages = [entry["content"] for entry in candidates]

            rerank_scores: list[float] = []
            if RERANKER_MODEL_FILE.exists():
                try:
                    loop = asyncio.get_event_loop()
                    rerank_scores = await loop.run_in_executor(
                        None, score_pairs, request.question, passages, RERANK_BATCH_SIZE
                    )
                    logger.info(
                        "[RERANK] question=%r tenant=%s candidates=%d scored=%d",
                        request.question[:100], tenant_id, len(passages), len(rerank_scores),
                    )
                except Exception as exc:
                    logger.warning("Reranker failed, falling back to hybrid order: %s", exc)
                    rerank_scores = []

            if rerank_scores:
                for idx, entry in enumerate(candidates):
                    entry["rerank_score"] = rerank_scores[idx]
                candidates.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)

            merged = candidates[:FINAL_LIMIT]

            context_parts = []
            sources = []
            for i, entry in enumerate(merged):
                metadata = entry["metadata"] or {}
                filename = metadata.get("filename", "unknown")
                chunk_index = metadata.get("chunk_index", 0)
                doc_id = metadata.get("document_id", "unknown")
                content = entry.get("content", "")

                tag = "BM25" if entry["bm25"] else "VECTOR"
                if "rerank_score" in entry:
                    tag += " + RERANK"
                    similarity = round(entry["rerank_score"], 4)
                else:
                    similarity = round(entry.get("combined_score", 0.0), 4)
                context_parts.append(f"[Source {i+1}] ({tag}) {content}")
                sources.append(
                    SourceReference(
                        filename=filename,
                        chunk_index=chunk_index,
                        document_id=doc_id,
                        similarity=similarity,
                        bm25=entry["bm25"],
                        reranked="rerank_score" in entry,
                    )
                )

            context = "\n\n---\n\n".join(context_parts)

            system_prompt = """You are a precise document-based Q&A assistant. Answer ONLY using the provided context chunks below. Do NOT use any outside knowledge.

RULES:
1. If the answer is not found in the context, say "I don't know" — do not guess or infer.
2. If the question asks to compare entities, find information about EACH entity in the context and compare them side by side. If an entity has no information in the context, say so explicitly.
3. If the question asks about a specific entity (e.g., "who is Seth?"), search ALL chunks for mentions of that entity and provide the relevant details.
4. Cite your sources using [Source N] references.
5. Be concise and factual. Do not add opinions.
6. If multiple chunks contain relevant info, synthesize them into a coherent answer.

Context:
{context}""".format(context=context)

            user_prompt = f"Question: {request.question}"
            chat_completion = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=1000,
            )
            answer = chat_completion.choices[0].message.content.strip()

        log_sql = text("""
            INSERT INTO audit_logs (tenant_id, user_id, question, answer, chunks_used)
            VALUES (:tenant_id, :user_id, :question, :answer, :chunks_used)
        """)
        await db.execute(
            log_sql,
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "question": request.question,
                "answer": answer,
                "chunks_used": json.dumps([{"document_id": s.document_id, "filename": s.filename, "chunk_index": s.chunk_index, "bm25": s.bm25} for s in sources]),
            },
        )
        await db.commit()

        return QueryResponse(answer=answer, sources=sources)

    except Exception as e:
        logger.error(f"Error processing query: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}")