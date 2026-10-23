"""把记忆核心的各部分装配在一起：身体（命令行、QQ、MC）只需要调用这里。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from companion.config import Config
from companion.core.context import ContextAssembler, ContextBundle
from companion.core.extract import Extraction, SessionExtractor, ended, split_sessions
from companion.core.ingest import Ingest
from companion.core.retrieval import Embedder, HybridRetriever, sync_vectors
from companion.memory.events import Event
from companion.memory.index import MemoryIndex
from companion.memory.layout import MemoryLayout, commit_memory, update_manifest
from companion.models.factory import build_embedder, build_gateway
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatResult, ModelError

log = logging.getLogger(__name__)

_CURSOR = "extract_cursor:"


@dataclass
class Reply:
    text: str
    result: ChatResult
    bundle: ContextBundle
    event: Event


class Companion:
    def __init__(
        self,
        config: Config,
        *,
        gateway: ModelGateway | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config
        self.name = config.persona.name
        self.layout = MemoryLayout(config.memory_dir)
        self.ingest = Ingest(self.layout)
        self.people = self.ingest.people
        self.log = self.ingest.log
        self.index = MemoryIndex(self.layout)
        self.embedder = embedder if embedder is not None else build_embedder(config)
        self.gateway = gateway if gateway is not None else build_gateway(config)
        emb_cfg = config.models.embedding
        self.retriever = HybridRetriever(
            self.index,
            self.embedder,
            query_instruction=emb_cfg.query_instruction,
            min_cosine=emb_cfg.min_cosine,
            text_weight=config.retrieval.text_weight,
        )
        self.context = ContextAssembler(
            persona_name=self.name,
            layout=self.layout,
            index=self.index,
            retriever=self.retriever,
            people=self.people,
            log=self.log,
            config=config.retrieval,
        )
        self.extractor = SessionExtractor(
            persona_name=self.name, gateway=self.gateway, index=self.index, people=self.people
        )

    def close(self) -> None:
        self.gateway.close()
        close = getattr(self.embedder, "close", None)
        if close:
            close()
        self.index.close()

    def __enter__(self) -> Companion:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 对话 ----

    def reply(
        self,
        *,
        body: str,
        channel: str,
        account: str,
        text: str,
        display_name: str | None = None,
        platform: str | None = None,
        now: datetime | None = None,
    ) -> Reply:
        """记录对方的话，组装上下文，生成并记录白璎的回复。"""
        incoming = self.ingest.record(
            body=body,
            channel=channel,
            kind="message",
            account=account,
            content=text,
            platform=platform,
            display_name=display_name,
            ts=now,
        )
        assert incoming.actor.person_id is not None
        bundle = self.context.build(
            person_id=incoming.actor.person_id, body=body, channel=channel, query=text, now=now
        )
        result = self.gateway.chat(bundle.messages, purpose="chat")
        self.context.mark_recalled(bundle, now=now)
        event = self.ingest.record_self(
            body=body,
            channel=channel,
            content=result.text,
            ts=now,
            meta={
                "backend": result.backend,
                "model": result.model,
                "recalled": [f"{r.kind}:{r.memory_id}" for r in bundle.recalled],
            },
        )
        return Reply(result.text, result, bundle, event)

    # ---- 整理记忆 ----

    def extract_pending(
        self, *, channel: str | None = None, force: bool = False, now: datetime | None = None
    ) -> list[Extraction]:
        """把已经结束的会话抽取成记忆。`force` 时不等空闲，立刻整理（如退出命令行时）。"""
        now = now or datetime.now(UTC)
        idle = timedelta(minutes=self.config.retrieval.session_idle_minutes)
        by_channel: dict[str, list[Event]] = {}
        for e in self.log.iter_events():
            if channel is not None and e.channel != channel:
                continue
            by_channel.setdefault(e.channel, []).append(e)

        results: list[Extraction] = []
        for ch, events in by_channel.items():
            pending = self._after_cursor(ch, events)
            for session in split_sessions(pending, idle):
                if not force and not ended(session, idle, now):
                    break
                extraction = self.extractor.extract(session)
                self._store(extraction)
                self._set_cursor(ch, session[-1])
                results.append(extraction)
        if results:
            self.sync_vectors()
            commit_memory(
                self.layout,
                self.name,
                f"整理记忆：{len(results)} 段会话（{now.astimezone():%m-%d %H:%M}）",
            )
        return results

    def sync_vectors(self) -> int:
        try:
            n = sync_vectors(self.index, self.embedder, self.config.models.embedding.batch_size)
        except ModelError as e:
            log.warning("向量暂时无法更新（关键词检索仍可用）：%s", e)
            return 0
        update_manifest(self.layout, embedding_model=self.embedder.model)
        return n

    def _store(self, extraction: Extraction) -> None:
        if extraction.episode is not None:
            self.index.add_episode(extraction.episode)
        for fact, supersedes in extraction.facts:
            self.index.add_fact(fact, supersedes=supersedes)

    def _after_cursor(self, channel: str, events: list[Event]) -> list[Event]:
        raw = self.index.get_meta(_CURSOR + channel)
        if raw is None:
            return events
        cursor = json.loads(raw)
        ids = [e.id for e in events]
        if cursor["id"] in ids:
            return events[ids.index(cursor["id"]) + 1 :]
        cut = datetime.fromisoformat(cursor["ts"])
        return [e for e in events if e.ts > cut]

    def _set_cursor(self, channel: str, last: Event) -> None:
        self.index.set_meta(
            _CURSOR + channel, json.dumps({"id": last.id, "ts": last.ts.isoformat()})
        )
