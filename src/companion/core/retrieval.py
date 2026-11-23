"""混合检索：关键词（FTS5）+ 向量 + 人物 + 时间。

两路结果用 RRF（倒数排名融合）合并。RRF 只看排名，所以另外返回向量余弦值，
供调用方过滤“名列前茅但其实不相关”的结果。embedding 服务不可用时退化为纯关键词检索。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np

from companion.memory.index import MemoryIndex, MemoryKind
from companion.models.openai_compat import ModelError

log = logging.getLogger(__name__)

RRF_K = 60

Key = tuple[MemoryKind, str]


class Embedder(Protocol):
    model: str | None

    def embed(self, texts: list[str]) -> np.ndarray: ...


@dataclass(frozen=True)
class Candidate:
    kind: MemoryKind
    memory_id: str
    relevance: float  # 0..1，RRF 归一化后的分数
    cosine: float | None  # 与查询的向量相似度；没有向量时为 None
    in_text: bool  # 关键词命中


def sync_vectors(index: MemoryIndex, embedder: Embedder, batch_size: int = 32) -> int:
    """给还没有向量（或向量来自旧模型）的记忆补算向量，返回补算条数。"""
    if not embedder.model:
        raise ModelError("embedding 没有配置模型名")
    pending = index.texts_needing_vectors(embedder.model)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        matrix = embedder.embed([text for _, _, text in batch])
        index.put_vectors(embedder.model, [(k, mid) for k, mid, _ in batch], matrix)
    return len(pending)


class HybridRetriever:
    def __init__(
        self,
        index: MemoryIndex,
        embedder: Embedder | None,
        *,
        query_instruction: str = "",
        min_cosine: float = 0.0,
        text_weight: float = 1.0,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self.query_instruction = query_instruction
        self.min_cosine = min_cosine
        self.text_weight = text_weight

    def search(
        self,
        query: str,
        *,
        person_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
        pool: int = 50,
    ) -> list[Candidate]:
        filtered = person_id is not None or since is not None or until is not None
        allowed = self.index.allowed_keys(person_id=person_id, since=since, until=until)
        if filtered and not allowed:
            return []
        scope = allowed if filtered else None

        text_ranked: list[Key] = [
            (h.kind, h.memory_id)
            for h in self.index.search_text(query, limit=pool * 4 if filtered else pool)
            if scope is None or (h.kind, h.memory_id) in scope
        ][:pool]
        vector_ranked, cosines = self._vector_search(query, scope, pool)

        scores: dict[Key, float] = {}
        # 向量可用时关键词只按 text_weight 参与；向量不可用时全靠关键词
        text_weight = self.text_weight if vector_ranked else 1.0
        weights = ((text_ranked, text_weight), (vector_ranked, 1.0))
        for ranked, weight in weights:
            for rank, key in enumerate(ranked, 1):
                scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank)
        best_possible = (text_weight + (1.0 if vector_ranked else 0.0)) / (RRF_K + 1)

        text_set = set(text_ranked)
        out: list[Candidate] = []
        for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
            record = (self.index.get_episode(key[1]) if key[0] == "episode"
                      else self.index.get_fact(key[1]))
            if record is None or not self.index.is_public(record):
                continue
            cos = cosines.get(key)
            if key not in text_set and cos is not None and cos < self.min_cosine:
                continue
            out.append(
                Candidate(key[0], key[1], min(score / best_possible, 1.0), cos, key in text_set)
            )
            if len(out) >= limit:
                break
        return out

    def _vector_search(
        self, query: str, scope: set[Key] | None, pool: int
    ) -> tuple[list[Key], dict[Key, float]]:
        if self.embedder is None or not self.embedder.model:
            return [], {}
        keys, matrix = self.index.load_vectors(self.embedder.model, scope)
        if not keys:
            return [], {}
        try:
            q = self.embedder.embed([self.query_instruction + query])[0]
        except ModelError as e:
            log.warning("向量检索不可用，退化为关键词检索：%s", e)
            return [], {}
        sims = matrix @ q
        order = np.argsort(-sims)[:pool]
        ranked = [keys[i] for i in order]
        return ranked, {keys[i]: float(sims[i]) for i in range(len(keys))}
