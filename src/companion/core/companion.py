"""把记忆核心的各部分装配在一起：身体（命令行、QQ、MC）只需要调用这里。"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from companion import media
from companion.config import Config
from companion.core.context import ContextAssembler, ContextBundle
from companion.core.extract import Extraction, SessionExtractor, ended, split_sessions
from companion.core.ingest import Ingest
from companion.core.retrieval import Embedder, HybridRetriever, sync_vectors
from companion.memory.events import Event
from companion.memory.index import Fact, MemoryIndex, MemoryKind
from companion.memory.layout import MemoryLayout, commit_memory, update_manifest
from companion.models.factory import build_embedder, build_gateway
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatResult, ModelError
from companion.stickers import Sticker, StickerBook, split_sticker

log = logging.getLogger(__name__)

_CURSOR = "extract_cursor:"


@dataclass
class Reply:
    text: str
    result: ChatResult
    bundle: ContextBundle
    event: Event
    sticker: Sticker | None = None  # 想配的表情包（已过概率与冷却）


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
        self.media = media.MediaStore(
            self.layout.media_dir,
            max_side=config.vision.max_side,
            max_bytes=config.vision.max_bytes,
        )
        self.stickers = StickerBook(
            self.layout.root / "stickers", capacity=config.stickers.capacity
        )
        self._last_sticker: dict[str, datetime] = {}
        self._rng = random.Random()

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
        return self.respond(incoming, now=now)

    def respond(self, incoming: Event, now: datetime | None = None) -> Reply:
        """对一条已经记下的消息作出回复。"""
        assert incoming.actor.person_id is not None
        body, channel = incoming.body, incoming.channel
        sticker_cfg = self.config.stickers
        emotions = (
            self.stickers.emotions()
            if sticker_cfg.enabled and len(self.stickers) >= sticker_cfg.min_library
            else []
        )
        bundle = self.context.build(
            person_id=incoming.actor.person_id,
            body=body,
            channel=channel,
            query=incoming.content,
            now=now,
            sticker_emotions=emotions,
        )
        result = self.gateway.chat(bundle.messages, purpose="chat")
        self.context.mark_recalled(bundle, now=now)
        text, wanted = split_sticker(result.text)
        sticker = self._choose_sticker(channel, wanted, now) if wanted and emotions else None
        event = self.ingest.record_self(
            body=body,
            channel=channel,
            content=text or "…",
            ts=now,
            meta={
                "backend": result.backend,
                "model": result.model,
                "recalled": [f"{r.kind}:{r.memory_id}" for r in bundle.recalled],
            },
        )
        if sticker is not None:
            self.ingest.record_self(
                body=body,
                channel=channel,
                content=f"[表情包：{sticker.description}]",
                ts=now,
                meta={"sticker": sticker.digest, "emotion": sticker.emotion},
            )
        return Reply(text, result, bundle, event, sticker)

    def _choose_sticker(self, channel: str, emotion: str, now: datetime | None) -> Sticker | None:
        cfg = self.config.stickers
        now = now or datetime.now(UTC)
        last = self._last_sticker.get(channel)
        if last is not None and (now - last).total_seconds() < cfg.min_interval_seconds:
            return None
        if self._rng.random() >= cfg.send_probability:
            return None
        sticker = self.stickers.pick(emotion, embed=self._embed_quietly, rng=self._rng)
        if sticker is not None:
            self._last_sticker[channel] = now
            self.stickers.mark_used(sticker.digest, now)
        return sticker

    def _embed_quietly(self, texts: list[str]):
        try:
            return self.embedder.embed(texts)
        except ModelError:
            return None

    def observe(
        self,
        *,
        body: str,
        channel: str,
        account: str,
        text: str,
        display_name: str | None = None,
        platform: str | None = None,
        now: datetime | None = None,
        meta: dict[str, object] | None = None,
    ) -> Event:
        """只记下来、不回复（如群里别人之间的聊天）。"""
        return self.ingest.record(
            body=body,
            channel=channel,
            kind="message",
            account=account,
            content=text,
            platform=platform,
            display_name=display_name,
            ts=now,
            meta=dict(meta or {}),
        )

    # ---- “记住这个”与“忘掉这件事” ----

    def remember(self, source: Event, note: str, now: datetime | None = None) -> Fact:
        """立刻记下并钉住一条内容，不等会话结束、也不参与遗忘。"""
        assert source.actor.person_id is not None
        pid = self.people.canonical(source.actor.person_id)
        who = self._display(pid)
        fact = Fact(
            subject=pid,
            predicate="asked_to_remember",
            statement=f"{who}让我记住：{note}",
            valid_from=source.ts,
            event_ids=[source.id],
            confidence=1.0,
            importance=1.0,
            pinned=True,
        )
        self.index.add_fact(fact, now=now)
        self.sync_vectors()
        return fact

    def forget_candidates(
        self, person_id: str, keyword: str, limit: int = 5
    ) -> list[tuple[MemoryKind, str, str]]:
        """和某人有关、与关键词相关的记忆，供 ta 挑选要忘掉的。"""
        pid = self.people.canonical(person_id)
        out: list[tuple[MemoryKind, str, str]] = []
        for c in self.retriever.search(keyword, person_id=pid, limit=limit * 2):
            text = self.index.text_of(c.kind, c.memory_id)
            if c.in_text or (c.cosine is not None and c.cosine >= self.retriever.min_cosine):
                out.append((c.kind, c.memory_id, text))
            if len(out) >= limit:
                break
        return out

    def forget(self, items: list[tuple[MemoryKind, str]]) -> tuple[int, int]:
        """彻底删除记忆及其出处消息；出处消息还支撑着的其他记忆一并删除。返回（记忆数，消息数）。"""
        to_delete = set(items)
        event_ids: set[str] = set()
        for kind, mid in items:
            event_ids |= set(self.index.delete(kind, mid))
        for kind, mid in self.index.memories_from_events(event_ids):
            if (kind, mid) not in to_delete:
                to_delete.add((kind, mid))
                event_ids |= set(self.index.delete(kind, mid))
        removed = self.log.redact(event_ids)
        self._drop_orphan_images(removed)
        commit_memory(self.layout, self.name, f"按要求忘掉了 {len(to_delete)} 条记忆")
        return len(to_delete), len(removed)

    # ---- 图片 ----

    def look(
        self, data: bytes, *, sticker: bool = False, person_id: str | None = None
    ) -> tuple[str, str]:
        """保存并让主模型看一张图，返回（描述，图片哈希）。

        表情包会同时判断情绪和是否适合收藏；适合的收进表情包库（群聊私聊共用）。
        """
        stored = self.media.save(data)
        if sticker and self.config.stickers.enabled:
            known = self.stickers.get(stored.digest)
            if known is not None:  # 收藏过的表情不用再看一遍
                self.stickers.add(
                    stored.path, stored.digest, known.description, known.emotion, person_id
                )
                return known.description, stored.digest
            description, emotion, usable = media.describe_sticker(self.gateway, stored.path)
            log.info("看表情包（%s，%s）：%s", emotion, "收藏" if usable else "不收藏", description)
            if usable:
                self.stickers.add(stored.path, stored.digest, description, emotion, person_id)
            return description, stored.digest
        description, backend = media.describe(self.gateway, stored.path)
        log.info("看图（%s）：%s", backend, description)
        return description, stored.digest

    def _drop_orphan_images(self, removed: list[Event]) -> None:
        """被删除的消息里的图片，如果没有别的消息还在用，就一起删掉。"""
        digests = {d for e in removed for d in e.meta.get("images", [])}
        if not digests:
            return
        still_used = {d for e in self.log.iter_events() for d in e.meta.get("images", [])}
        for digest in digests - still_used:
            self.media.delete(digest)

    def _display(self, person_id: str) -> str:
        person = self.people.get(person_id)
        return person.names[0] if person and person.names else person_id

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
            events.sort(key=lambda e: e.ts)  # 看图会让写入顺序略晚于发生顺序
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
