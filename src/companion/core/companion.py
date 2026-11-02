"""把记忆核心的各部分装配在一起：身体（命令行、QQ、MC）只需要调用这里。"""

from __future__ import annotations

import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion import media
from companion.config import Config
from companion.core.actions import Actions, split_actions
from companion.core.context import ContextAssembler, ContextBundle
from companion.core.extract import Extraction, SessionExtractor, ended, split_sessions
from companion.core.ingest import Ingest
from companion.core.retrieval import Embedder, HybridRetriever, sync_vectors
from companion.life import Life, split_mood
from companion.life.activities import (
    LIFE_INSTRUCTIONS,
    ActivityPrep,
    ActivityResult,
    ActivityRunner,
    LifeBackend,
)
from companion.life.core import LIFE_CHANNEL
from companion.memory.events import Event
from companion.memory.index import Fact, MemoryIndex, MemoryKind
from companion.memory.layout import MemoryLayout, commit_memory, update_manifest
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.factory import build_embedder, build_gateway
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError
from companion.skills import Skill, SkillCall, SkillContext, SkillResult, Skills
from companion.skills.base import MARKER, SkillError
from companion.skills.builtin import build_skills
from companion.skills.files import SendFile
from companion.skills.reminder import Reminder, ReminderBook
from companion.stickers import Sticker, StickerBook, split_sticker

log = logging.getLogger(__name__)

_CURSOR = "extract_cursor:"
SILENT_MARK = re.compile(r"[【\[]\s*(不回复|SILENT)\s*[】\]]", re.IGNORECASE)


REMINDER_LATE = timedelta(minutes=5)  # 迟了超过这么久，就让她说明迟到了
_GREETINGS = {
    "morning": "（现在是早上，你想主动跟{who}说声早安。可以顺口提一句今天打算做什么；"
    "一两句话，别连着问问题。）",
    "night": "（夜深了，你想主动跟{who}道声晚安。可以提一句今天真实发生的事或你自己做的事；"
    "一两句话。）",
}


def _said(text: str, actions: Actions) -> str:
    """记进日志的“她说了什么”：话本身，加上用声音说的、回应和戳一戳。"""
    parts = [text] if text else []
    if actions.voice:
        parts.append(f"（用语音说）{actions.voice}")
    if actions.reaction:
        parts.append(f"[给这条消息点了 {actions.reaction}]")
    if actions.poke:
        parts.append("[戳了戳对方]")
    return " ".join(parts) or "…"


def _share_note(result: ActivityResult, who: str, with_image: bool) -> str:
    """自主活动做完、想告诉主人时的说明。"""
    if result.kind == "chat_owner":
        return (
            f"（你想主动找{who}聊聊：{result.topic}（{result.why}）。"
            "自然地开个头，一两句话；不合适就只输出【不回复】。）"
        )
    if result.kind == "write":
        detail = f"你写的：\n{result.text}\n想拿给{who}看，可以把它贴出来，再随口说一句。"
    elif with_image:
        detail = "画好了，图会跟着你的话一起发过去。"
    else:
        detail = f"你查到的：{result.text}"
    return (
        f"（你刚才自己{result.title}。{detail}\n"
        f"你想随口跟{who}分享一下：用你的语气，像顺口一提，不要像汇报；"
        "不值得打扰就只输出【不回复】。）"
    )


@dataclass
class Reply:
    text: str
    result: ChatResult
    bundle: ContextBundle
    event: Event
    sticker: Sticker | None = None  # 想配的表情包（已过概率与冷却）
    silent: bool = False  # 她判断这一轮不需要接话
    skill_calls: list[SkillCall] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)  # 技能产出、要发给对方的图片
    files: list[Path] = field(default_factory=list)  # 要发给对方的文件（【发文件：…】）
    actions: Actions = field(default_factory=Actions)  # 表情回应、戳一戳、用声音说的话
    skill_ctx: SkillContext | None = None

    @property
    def later(self) -> list[SkillCall]:
        """要放到后台做的活（如画图、Agent 任务）。做完后：结果带 report 的用 Companion.report
        让她转述；否则用 Companion.deliver 记下并把图片发出去。"""
        return [c for c in self.skill_calls if c.later is not None]


class Companion:
    def __init__(
        self,
        config: Config,
        *,
        gateway: ModelGateway | None = None,
        embedder: Embedder | None = None,
        skills: list[Skill] | None = None,
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
        self.life = Life(
            config=config, layout=self.layout, index=self.index, gateway=self.gateway, log=self.log
        )
        self.activities = ActivityRunner(self._life_backend_factory())
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
            life=self.life,
            qq=config.qq,
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
        self.reminders = ReminderBook(config.data_dir / "reminders.json")
        if skills is None:
            skills = build_skills(config, self.gateway, self.reminders)
        self.skills = Skills(skills, config.skills, config.data_dir / "skills.jsonl")

    def close(self) -> None:
        self.activities.close()
        self.skills.close()
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
        on_interim: Callable[[str], None] | None = None,
        ask: Callable[[str], bool] | None = None,
    ) -> Reply:
        """记录对方的话，组装上下文，生成并记录白璎的回复（本地命令行：当作主人私聊）。

        on_interim：她先说的话、后台任务进度；ask：后台任务请求批准时问主人。"""
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
        ctx = SkillContext(body, channel, incoming.actor.person_id, private=True, owner=True,
                           now=now, notify=on_interim, ask=ask)  # fmt: skip
        return self.respond(incoming, now=now, skill_ctx=ctx, on_interim=on_interim)

    def respond(
        self,
        incoming: Event,
        now: datetime | None = None,
        *,
        mode: str = "required",
        batch: list[Event] | None = None,
        skill_ctx: SkillContext | None = None,
        on_interim: Callable[[str], None] | None = None,
    ) -> Reply:
        """对一条（或接连几条合并成的一轮）已经记下的消息作出回复。

        mode：required = 被直接叫到，一般要回；optional = 可以不接；
              strict = 不是冲她来的，更倾向不接。
        她判断不需要接话时会输出【不回复】，此时返回 silent=True，不记录也不发送。
        skill_ctx：这个场合能用哪些技能（None = 不用技能）。她用技能前先说的话
        （如“我查一下”）会记下来并交给 on_interim 立刻发出去。
        """
        assert incoming.actor.person_id is not None
        body, channel = incoming.body, incoming.channel
        query = "\n".join(e.content for e in batch) if batch else incoming.content
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
            query=query,
            now=now,
            sticker_emotions=emotions,
            reply_mode=mode,
            extra_sections=[p] if (p := self.skills.prompt(skill_ctx)) else None,
        )
        result, calls = self._chat_with_skills(bundle, skill_ctx, on_interim)
        text = self._take_mood(
            self.skills.strip_markers(result.text), channel, incoming.actor.person_id, now
        )
        text, wanted = split_sticker(text)
        text, actions = split_actions(text)
        if SILENT_MARK.search(text):  # 说了不回复：话和表情包都不发，但回应、戳一戳照做
            text, wanted = "", None
        images = [p for c in calls for p in c.images]
        files = [p for c in calls for p in c.files]
        if not images and not files and not actions and not text.strip() and not wanted:
            return Reply("", result, bundle, incoming, None, silent=True, skill_calls=calls,
                         skill_ctx=skill_ctx)  # fmt: skip
        text = SILENT_MARK.sub("", text).strip()
        self.context.mark_recalled(bundle, now=now)
        sticker = self._choose_sticker(channel, wanted, now) if wanted and emotions else None
        meta: dict[str, object] = {
            "backend": result.backend,
            "model": result.model,
            "recalled": [f"{r.kind}:{r.memory_id}" for r in bundle.recalled],
        }
        if calls:
            meta["skills"] = [{"skill": c.skill, "arg": c.arg[:200], "ok": c.ok} for c in calls]
        event = self.ingest.record_self(
            body=body, channel=channel, content=_said(text, actions), ts=now, meta=meta
        )
        if sticker is not None:
            self._record_sticker(body, channel, sticker, now)
        self._record_files(body, channel, files, now)
        return Reply(text, result, bundle, event, sticker, skill_calls=calls, images=images,
                     files=files, skill_ctx=skill_ctx, actions=actions)  # fmt: skip

    def _chat_with_skills(
        self,
        bundle: ContextBundle,
        ctx: SkillContext | None,
        on_interim: Callable[[str], None] | None,
    ) -> tuple[ChatResult, list[SkillCall]]:
        """生成回复；她写了技能标记就执行，把结果交回给她，直到她给出最终回复。"""
        messages = list(bundle.messages)
        calls: list[SkillCall] = []
        rounds = self.config.skills.max_rounds
        while True:
            result = self.gateway.chat(messages, purpose="chat")
            found = self.skills.find(result.text, ctx) if len(calls) < rounds else None
            if found is None:
                return result, calls
            before, skill, marker, arg = found
            assert ctx is not None
            interim = split_actions(split_sticker(split_mood(before)[0])[0])[0]
            interim = SILENT_MARK.sub("", interim).strip()
            if interim:
                self.ingest.record_self(
                    body=ctx.body, channel=ctx.channel, content=interim, ts=ctx.now
                )
                if on_interim is not None:
                    on_interim(interim)
            call, outcome = self.skills.run(skill, marker, arg, ctx)
            calls.append(call)
            said = f"{before}【{marker}：{arg}】" if before else f"【{marker}：{arg}】"
            messages += [
                ChatMessage("assistant", said),
                ChatMessage("skill", self.skills.feedback(skill, marker, outcome)),
            ]

    def deliver(
        self, body: str, channel: str, result: SkillResult, now: datetime | None = None
    ) -> list[Path]:
        """后台技能做完了：把图片存进图片库、记成她发的消息，返回要发出去的图片路径。"""
        if not result.ok:
            return []
        paths, digests = [], []
        for image in result.images:
            try:
                stored = self.media.save(image.read_bytes())
            except (OSError, media.MediaError) as e:
                log.warning("保存技能图片失败：%s", e)
                continue
            paths.append(stored.path)
            digests.append(stored.digest)
        if digests:
            self.ingest.record_self(
                body=body,
                channel=channel,
                content=f"[我画的图：{media.clean(result.text)}]",
                ts=now,
                meta={"images": digests},
            )
        return paths

    def report(
        self, ctx: SkillContext, skill_name: str, result: SkillResult
    ) -> tuple[str, list[Path]]:
        """后台任务做完了：让她用自己的话把结果告诉对方，记下并返回（这句话，要一起发的文件）。

        这个场合能发文件时，她可以在话里写【发文件：路径】把产出的文件直接发过去。"""
        label = next((s.label for s in self.skills.all if s.name == skill_name), skill_name)
        bundle = self.context.build(
            person_id=ctx.person_id,
            body=ctx.body,
            channel=ctx.channel,
            query=result.text[:300],
            reminder=False,
        )
        done = "完成了" if result.ok else "没有完成"
        note = (
            f"【{label}{done}】\n{result.text[: self.config.skills.max_result_chars]}\n\n"
            "这是你之前交给后台的任务的结果，不是对方说的话。请用你自己的话简洁地告诉对方："
            "说重点和产出文件在哪；没做成就说明原因。不要编造结果里没有的内容。"
        )
        sender = self._file_sender(ctx)
        if sender is not None:
            note += (
                "\n如果对方用得上产出的文件，可以在话里单独写【发文件：工作文件夹里的路径】"
                f"直接发过去（{self.config.skills.files.max_mb:g} MB 以内）。"
            )
        files: list[Path] = []
        try:
            answer = self.gateway.chat([*bundle.messages, ChatMessage("skill", note)])
            files = self._files_in(answer.text, sender)
            text = self._take_mood(
                self.skills.strip_markers(answer.text), ctx.channel, ctx.person_id, None
            )
            text = split_actions(split_sticker(text)[0])[0]
            text = SILENT_MARK.sub("", text).strip()
        except ModelError as e:
            log.warning("转述任务结果失败：%s", e)
            text = ""
        text = text or f"{label}{done}：{result.text[:400]}"
        self.ingest.record_self(
            body=ctx.body, channel=ctx.channel, content=text, meta={"report": skill_name}
        )
        self._record_files(ctx.body, ctx.channel, files, None)
        return text, files

    def _file_sender(self, ctx: SkillContext) -> SendFile | None:
        skill = next((s for s in self.skills.available(ctx) if isinstance(s, SendFile)), None)
        return skill

    def _files_in(self, text: str, sender: SendFile | None) -> list[Path]:
        """她话里的【发文件：…】：在工作文件夹里找得到、大小合适的才发。"""
        if sender is None:
            return []
        files = []
        for match in MARKER.finditer(text):
            if match.group(1) in sender.markers:
                try:
                    files.append(sender.resolve(match.group(2)))
                except SkillError as e:
                    log.info("没发文件：%s", e)
        return list(dict.fromkeys(files))

    def _record_files(
        self, body: str, channel: str, files: list[Path], now: datetime | None
    ) -> None:
        for path in files:
            self.ingest.record_self(
                body=body, channel=channel, content=f"[我发的文件：{path.name}]", ts=now,
                meta={"file": str(path)},
            )  # fmt: skip

    # ---- 主动开口（只在主人私聊里） ----

    def greet(
        self, *, person_id: str, body: str, channel: str, kind: str, now: datetime | None = None
    ) -> Reply | None:
        """早安（morning）或晚安（night）。"""
        note = _GREETINGS[kind].format(who=self._display(person_id))
        return self.initiate(
            person_id=person_id, body=body, channel=channel, kind=kind, note=note, now=now
        )

    def initiate(
        self,
        *,
        person_id: str,
        body: str,
        channel: str,
        kind: str,
        note: str,
        topic: str = "",
        now: datetime | None = None,
    ) -> Reply | None:
        """她主动说一句（早晚安、分享）。她觉得不合适打扰、或生成失败时返回 None，记为跳过。"""
        now = now or datetime.now(UTC)
        cfg = self.config.stickers
        emotions = (
            self.stickers.emotions()
            if cfg.enabled and len(self.stickers) >= cfg.min_library
            else []
        )
        bundle = self.context.build(
            person_id=person_id,
            body=body,
            channel=channel,
            query=topic or note,
            now=now,
            sticker_emotions=emotions,
            reply_mode="initiative",
            initiative=note,
        )
        try:
            result = self.gateway.chat(bundle.messages, purpose="initiative")
        except ModelError as e:
            log.warning("主动开口失败：%s", e)
            self.life.owner.record(now, kind, "", skipped=True)
            return None
        text = self._take_mood(self.skills.strip_markers(result.text), channel, person_id, now)
        text, wanted = split_sticker(text)
        text, actions = split_actions(text)
        actions.reaction, actions.poke = None, False  # 主动开口时没有可回应、可戳的消息
        if SILENT_MARK.search(text) or not (text.strip() or actions.voice):
            log.info("想主动找 %s（%s），想了想没开口", self._display(person_id), kind)
            self.life.owner.record(now, kind, "", skipped=True)
            return None
        sticker = self._choose_sticker(channel, wanted, now) if wanted and emotions else None
        meta = {"proactive": kind, "backend": result.backend, "model": result.model}
        said = _said(text, actions)
        event = self.ingest.record_self(body=body, channel=channel, content=said, ts=now, meta=meta)
        if sticker is not None:
            self._record_sticker(body, channel, sticker, now)
        self.life.owner.record(now, kind, said)
        return Reply(text, result, bundle, event, sticker, actions=actions)

    # ---- 定时提醒 ----

    def remind_text(self, reminder: Reminder, now: datetime) -> tuple[str, Sticker | None]:
        """到点了：让她用自己的话提醒。这时还没发出去，发成功后再调 reminder_sent 记下。

        模型不可用或她没写出来时，退回一句直白的话：提醒比措辞重要。
        """
        who = self._display(reminder.person_id)
        late = now - reminder.due
        lateness = ""
        if late > REMINDER_LATE:  # 服务没开、NapCat 断线时错过的
            lateness = (
                f"这条本该在 {reminder.due.astimezone():%m月%d日 %H:%M} 提醒，现在已经迟了，"
                "先说明一下迟到了。"
            )
        note = (
            f"（到时间了。{who}之前让你提醒：{reminder.what}。{lateness}"
            "用你自己的语气提醒 ta，一两句话，把要做的事说清楚；"
            "可以结合你们的关系和你现在的心情，不要说成“提醒您”这样的通知。）"
        )
        fallback = f"到时间啦，提醒你：{reminder.what}", None
        cfg = self.config.stickers
        emotions = (
            self.stickers.emotions()
            if cfg.enabled and len(self.stickers) >= cfg.min_library
            else []
        )
        bundle = self.context.build(
            person_id=reminder.person_id,
            body="qq",
            channel=reminder.channel,
            query=reminder.what,
            now=now,
            sticker_emotions=emotions,
            reply_mode="reminder",
            initiative=note,
        )
        try:
            result = self.gateway.chat(bundle.messages, purpose="reminder")
        except ModelError as e:
            log.warning("写提醒的话失败，直接提醒：%s", e)
            return fallback
        text = self._take_mood(
            self.skills.strip_markers(result.text), reminder.channel, reminder.person_id, now
        )
        text, wanted = split_sticker(text)
        text, actions = split_actions(text)
        if actions.voice:  # 私聊发不了语音
            text = f"{text} {actions.voice}".strip()
        text = SILENT_MARK.sub("", text).strip()
        if not text:
            return fallback
        sticker = (
            self._choose_sticker(reminder.channel, wanted, now) if wanted and emotions else None
        )
        return text, sticker

    def reminder_sent(
        self, reminder: Reminder, text: str, sticker: Sticker | None, now: datetime
    ) -> None:
        """提醒发出去了：记成她说的话，一次性的提醒就此结束，重复的排到下一次。"""
        self.ingest.record_self(
            body="qq", channel=reminder.channel, content=text, ts=now,
            meta={"reminder": reminder.id},
        )  # fmt: skip
        if sticker is not None:
            self._record_sticker("qq", reminder.channel, sticker, now)
        self.reminders.finish(reminder.id, now)

    # ---- 额度充足时的自主活动 ----

    def prepare_activity(
        self,
        now: datetime | None = None,
        *,
        owner_id: str | None = None,
        owner_channel: str | None = None,
    ) -> ActivityPrep | None:
        """该不该现在做点自己的事；该做就返回挑事情的材料（交给 activities.perform 在后台做）。"""
        if not self.activities.available:
            return None
        now = now or datetime.now(UTC)
        owner_ok = False
        if owner_id and owner_channel:
            owner_ok = self.life.owner.can_share(owner_id, owner_channel, now)[0]
        return self.life.prepare_activity(
            now, owner_name=self._display(owner_id) if owner_id else None, owner_ok=owner_ok
        )

    def finish_activity(
        self,
        result: ActivityResult | None,
        now: datetime | None = None,
        *,
        owner_id: str | None = None,
        owner_channel: str | None = None,
        body: str = "qq",
    ) -> tuple[Reply | None, list[Path]]:
        """记下做完的事（生活记录、记忆、心情）；值得分享且不打扰时，主动告诉主人。
        返回（要发给主人的话，要一起发的图片）。"""
        now = now or datetime.now(UTC)
        self.life.activity_done(now)
        if result is None or result.kind == "rest":
            return None, []
        digests: list[str] = []
        paths: list[Path] = []
        if result.image is not None:
            try:
                stored = self.media.save(result.image.read_bytes())
                digests, paths = [stored.digest], [stored.path]
            except (OSError, media.MediaError) as e:
                log.warning("保存自己画的图失败：%s", e)
        self.life.record_activity(result, now, image=digests[0] if digests else None)
        meta: dict[str, object] = {"activity": result.kind, "topic": result.topic}
        if digests:
            meta["images"] = digests
        if result.sources:
            meta["sources"] = result.sources
        self.ingest.record_self(
            body="life",
            channel=LIFE_CHANNEL,
            kind="action",
            content=f"[{result.title}] {result.text}".strip()[:800],
            ts=now,
            meta=meta,
        )
        if result.mood is not None:
            self.life.note_mood(*result.mood, channel=LIFE_CHANNEL, person_id=None, now=now)
        if not (result.share and owner_id and owner_channel):
            return None, []
        ok, why = self.life.owner.can_share(owner_id, owner_channel, now)
        if not ok:
            log.info("想跟主人分享“%s”，但先不打扰：%s", result.title, why)
            return None, []
        reply = self.initiate(
            person_id=owner_id,
            body=body,
            channel=owner_channel,
            kind="chat_owner" if result.kind == "chat_owner" else "share",
            note=_share_note(result, self._display(owner_id), bool(paths)),
            topic=result.topic,
            now=now,
        )
        if reply is None or not paths:
            return reply, []
        self.ingest.record_self(
            body=body,
            channel=owner_channel,
            content=f"[我画的图：{result.topic}]",
            ts=now,
            meta={"images": digests},
        )
        return reply, paths

    def _life_backend_factory(self) -> Callable[[], LifeBackend] | None:
        """做自主活动用的 Codex 进程（能联网搜索、能画图）；没有订阅就没有自主活动。"""
        if self.gateway.subscription is None:
            return None
        sub = self.config.models.subscription
        timeout = self.config.life.activities.timeout_seconds
        workdir = self.config.data_dir / "codex-life"
        instructions = LIFE_INSTRUCTIONS.format(name=self.name)

        def make() -> LifeBackend:
            return CodexAppServerBackend.from_codex(
                sub.codex_command,
                workdir,
                overrides={"features.shell_tool": "false", "web_search": '"live"'},
                model=sub.model,
                effort="low",
                timeout=timeout,
                name="life",
                instructions=instructions,
            )

        return make

    def _record_sticker(
        self, body: str, channel: str, sticker: Sticker, now: datetime | None
    ) -> None:
        self.ingest.record_self(
            body=body,
            channel=channel,
            content=f"[表情包：{sticker.description}]",
            ts=now,
            meta={"sticker": sticker.digest, "emotion": sticker.emotion},
        )

    def _take_mood(
        self, text: str, channel: str, person_id: str | None, now: datetime | None
    ) -> str:
        """拿掉回复里的【心情：…】；写了就记下她的心情变化（沉默时也算）。"""
        text, mood = split_mood(text)
        if mood is not None:
            self.life.note_mood(*mood, channel=channel, person_id=person_id, now=now)
        return text

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
