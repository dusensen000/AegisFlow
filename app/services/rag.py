"""轻量 RAG 检索器，作为规则与 FAQ 的事实锚点。"""

from __future__ import annotations

import re
import hashlib
import json

from app.services.cache import Cache


class SimpleRAGRetriever:
    def __init__(
        self,
        documents: list[dict],
        top_k: int = 5,
        cache: Cache | None = None,
    ):
        self.documents = documents
        self.top_k = top_k
        self.cache = cache
        self.version = hashlib.sha256(json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    async def aretrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """带缓存的热点知识检索。"""
        key = f"rag:{self.version}:{query}:{top_k or self.top_k}"
        if self.cache is not None:
            cached = await self.cache.get(key)
            if cached is not None:
                return cached
        hits = self.retrieve(query, top_k)
        if self.cache is not None:
            await self.cache.set(key, hits, ttl_seconds=300)
        return hits

    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        k = top_k or self.top_k
        query_tokens = set(self._tokenize(query))
        scored: list[tuple[float, dict]] = []
        for doc in self.documents:
            text = f"{doc.get('title', '')} {doc.get('content', '')}"
            tokens = self._tokenize(text)
            overlap = sum(min(tokens.count(token), 3) for token in query_tokens)
            score = overlap / max(1, len(query_tokens))
            if doc.get("category") in query:
                score += 2.0
            if doc.get("id", "").lower() in query.lower():
                score += 5.0
            scored.append((score, doc))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "id": doc["id"],
                "title": doc.get("title", ""),
                "content": doc.get("content", ""),
                "score": round(score, 4),
                "source": doc.get("source", "sop/manual"),
                "version": self.version,
            }
            for score, doc in scored[:k]
            if score > 0
        ]

    def format_context(self, hits: list[dict]) -> str:
        return "\n".join(
            f"[{item['id']}] {item['title']}：{item['content']}"
            for item in hits
        )

    def with_references(self, hits: list[dict], refs: set[str]) -> list[dict]:
        output = {h["id"]: h for h in hits}
        for doc in self.documents:
            if doc["id"] in refs:
                output[doc["id"]] = {**doc, "score": 1.0, "version": self.version,
                                     "source": doc.get("source", "sop/manual")}
        return list(output.values())

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        text = text.lower()
        chunks = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text)
        tokens = []
        for chunk in chunks:
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                tokens.extend(chunk[i:i+2] for i in range(max(1, len(chunk)-1)))
            else:
                tokens.append(chunk)
        return tokens
