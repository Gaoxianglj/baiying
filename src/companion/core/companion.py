"""把记忆核心的各部分装配在一起：身体（命令行、QQ、MC）只需要调用这里。"""

from __future__ import annotations

import contextlib
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
from companion.core.context import _STYLE_RULES, ContextAssembler, ContextBundle
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
from companion.memory.layout import MemoryLayout, commit_memory, ensure_gitignore, update_manifest
from companion.models.codex_app_server import CodexAppServerBackend, talk_only_overrides
from companion.models.factory import build_embedder, build_gateway
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError
from companion.skills import Skill, SkillCall, SkillContext, SkillResult, Skills
from companion.skills.background import run_background
from companion.skills.base import MARKER, SkillError
from companion.skills.builtin import build_skills
from companion.skills.files import SendFile
from companion.skills.native import NativeSkills
from companion.skills.reminder import Promise, PromiseBook, Reminder, ReminderBook
from companion.skills.tasks import TaskBook, TaskControl
from companion.skills.trpg import Trpg
from companion.stickers import Sticker, StickerBook, split_sticker
from companion.trpg.controller import Controller as TrpgController

log = logging.getLogger(__name__)

_CURSOR = "extract_cursor:"
SILENT_MARK = re.compile(r"[【\[]\s*(不回复|SILENT)\s*[】\]]", re.IGNORECASE)
IMAGE_REF_NOTE = re.compile(r"（图片编号：[^）]*）")  # 程序标的，万一被她照抄了也不发出去


REMINDER_LATE = timedelta(minutes=5)  # 迟了超过这么久，就让她说明迟到了
PROMISE_SAFE_SKILLS = {
    "search",
    "image",
    "pictures",
    "files",
    "send_files",
    "read_files",
    "export_text",
    "write_files",
    "find_files",
    "history",
    "document_pages",
}
_PACT_QUERY_RE = re.compile(
    r"约定|约好|一言为定|以后每天|每天都?(?:画|写|发|记|存|提醒|叫)|"
    r"每(?:周|星期)[一二三四五六日天]?都?(?:画|写|发|记|存|提醒|叫)|"
    r"以后(?:别|不要|不许)(?:再)?(?:发|提|说)"
)
_PACT_ACCEPT_RE = re.compile(
    r"约好|一言为定|记下(?:了|啦|这个约定)|以后每天|每天都|每周都|绝不(?:会)?(?:再)?发|不会再发"
)
_PACT_CANCEL_QUERY_RE = re.compile(
    r"(?:解除|取消|停掉|不用再|别再|先停).{0,12}(?:约定|提醒|画|花册|写)"
)
_PACT_CANCEL_ACCEPT_RE = re.compile(r"解除|取消|停下|先停|不画了|不再提醒|那就先不")


@dataclass
class PromiseOutcome:
    ok: bool = True
    text: str = ""
    sticker: Sticker | None = None
    images: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    summary: str = ""
    defer_minutes: int = 0
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
        ensure_gitignore(self.layout)
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
        self.reminders: PromiseBook = ReminderBook(config.data_dir / "reminders.json")
        if skills is None:
            skills = build_skills(
                config, self.gateway, self.reminders, self.log, self.media, people=self.people
            )
        self.tasks = TaskBook(config.data_dir / "run" / "tasks.json")
        self.background_gateway_factory = lambda: build_gateway(self.config, task_mode=True)
        self.skills = Skills(skills, config.skills, config.data_dir / "skills.jsonl", self.tasks)
        # 放在最前面：已有的主人助手或群沙箱仍优先处理它们允许场合的【任务】。
        self.skills.all.insert(0, TaskControl(self.tasks, self._background_work, self._resume_task))
        self.trpg = TrpgController(config)
        self.trpg.person_resolver = lambda account: self.people.ensure("qq", account)
        self.trpg.narrator.persona_provider = lambda: (
            "\n".join(
                self.context._read_self(name)[:400]
                for name in ("seed.md", "identity.md", "style.md")
            )
            + "\n"
            + _STYLE_RULES
        )
        self.skills.all.append(Trpg(self.trpg))

    def _background_work(self, task: str, ctx: SkillContext) -> SkillResult:
        return run_background(
            task,
            ctx,
            self.config,
            self.tasks,
            self.background_gateway_factory,
            lambda gateway: [
                *build_skills(
                    self.config,
                    gateway,
                    self.reminders,
                    self.log,
                    self.media,
                    people=self.people,
                ),
                Trpg(self.trpg),
            ],
        )

    def _resume_task(self, row: dict, ctx: SkillContext) -> SkillResult:
        skill = next((s for s in self.skills.available(ctx) if s.name == row["skill"]), None)
        if skill is None:
            raise SkillError("原任务使用的功能当前不可用，不能继续")
        return skill.run(row["marker"], row["arg"], ctx)

    def close(self) -> None:
        self.trpg.close()
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
        if skill_ctx is not None:
            skill_ctx.query = query
        skill_prompt, skill_notes = self.skills.prompt_parts(skill_ctx)
        session_skill_prompt, session_skill_notes = self.skills.session_prompt_parts(skill_ctx)
        bundle = self.context.build(
            person_id=incoming.actor.person_id,
            body=body,
            channel=channel,
            query=query,
            now=now,
            sticker_emotions=emotions,
            reply_mode=mode,
            extra_sections=[skill_prompt] if skill_prompt else None,
            dynamic_sections=skill_notes,
            session_extra_sections=[session_skill_prompt] if session_skill_prompt else [],
            session_dynamic_sections=session_skill_notes,
            show_image_refs=self.skills.can_use("看图", skill_ctx),
        )
        result, calls = self._chat_with_skills(bundle, skill_ctx, on_interim)
        text = self._take_mood(
            self.skills.strip_markers(result.text), channel, incoming.actor.person_id, now
        )
        text = IMAGE_REF_NOTE.sub("", text)
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
            if any(c.skill == "trpg" for c in calls):
                meta["fiction"] = True
        event = self.ingest.record_self(
            body=body, channel=channel, content=_said(text, actions), ts=now, meta=meta
        )
        self.gateway.note_response(result, event.id)
        if sticker is not None:
            self._record_sticker(body, channel, sticker, now)
        self._record_sent_images(body, channel, images, now)
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
        temporary: list[Path] = []  # 这一轮说完就删的图（屏幕、剪贴板）
        session_key = bundle.session_key
        session_messages = list(bundle.session_messages or messages)
        session_bootstrap = list(session_messages)
        turn_tail = [
            m for m in (bundle.session_messages or []) if m.role == "system" and m.event_id is None
        ]
        session_prefix_and_history = [
            m
            for m in (bundle.session_messages or messages)
            if not (m.role == "system" and m.event_id is None)
        ]

        def native_result(call: SkillCall, outcome: SkillResult) -> None:
            calls.append(call)

        bridge = (
            NativeSkills(self.skills, ctx, native_result)
            if ctx and self.config.models.subscription.native_tools
            else None
        )
        discovery_retry = False
        promise_retry = False
        try:
            while True:
                if ctx and ctx.stop_event and ctx.task_id and ctx.stop_event.is_set():
                    raise SkillError("任务已停止")
                result = self.gateway.chat(
                    messages,
                    purpose="chat",
                    session_key=session_key,
                    session_messages=session_messages,
                    session_bootstrap=session_bootstrap,
                    tools=bridge.tools if bridge else None,
                )
                found = self.skills.find(result.text, ctx)
                if found is None:
                    file_request = bool(
                        ctx
                        and re.search(
                            r"(?:读|找|打开|载入|导入|使用|用这|用之前|能看到|看得到|还记得|查).{0,35}"
                            r"(?:文件|附件|规则|模板|人物卡|角色卡|这份|这些)|\.(?:pdf|xlsx|md|docx|zip)",
                            ctx.query,
                            re.I,
                        )
                    )
                    refusing_file = re.search(
                        r"看不到|读不到|重新.{0,8}(?:上传|发送|附上)|补发|贴.{0,8}(?:正文|内容)|没有.{0,8}正文",
                        result.text,
                    )
                    used_discovery = any(
                        c.skill == "find_files" or (c.skill == "read_files" and c.ok) for c in calls
                    )
                    if (
                        file_request
                        and refusing_file
                        and not used_discovery
                        and not discovery_retry
                    ):
                        discovery_retry = True
                        reminder = ChatMessage(
                            "skill",
                            "请先在当前聊天调用找文件或看目录并读取实际资料；"
                            "若本地缺少且提供了群文件工具，再查当前QQ群文件。完成查找前不要要求重传或贴正文。",
                        )
                        messages.append(reminder)
                        session_messages = (
                            [reminder, *turn_tail]
                            if result.raw.get("session_key") == session_key and session_key
                            else [*session_prefix_and_history, reminder, *turn_tail]
                        )
                        session_bootstrap.append(reminder)
                        continue
                    if (
                        ctx
                        and not promise_retry
                        and self.skills.can_use("约定", ctx)
                        and not any(c.skill == "reminders" for c in calls)
                    ):
                        missed_create = bool(
                            _PACT_QUERY_RE.search(ctx.query) and _PACT_ACCEPT_RE.search(result.text)
                        )
                        missed_cancel = bool(
                            self.reminders.pending(ctx.channel)
                            and _PACT_CANCEL_QUERY_RE.search(ctx.query)
                            and _PACT_CANCEL_ACCEPT_RE.search(result.text)
                        )
                        if missed_create or missed_cancel:
                            promise_retry = True
                            nudge = ChatMessage(
                                "skill",
                                "你刚才在回复里答应了建立、调整或解除约定，但还没有调用【约定】、"
                                "【修改约定】或【解除约定】写入约定本。若确实达成了约定或解约，"
                                "请先调用对应工具再回复；若无需变更约定本，请直接给出回复且不要口头承诺自动执行。",
                            )
                            messages.append(nudge)
                            session_messages = (
                                [nudge, *turn_tail]
                                if result.raw.get("session_key") == session_key and session_key
                                else [*session_prefix_and_history, nudge, *turn_tail]
                            )
                            session_bootstrap.append(nudge)
                            continue
                    return result, calls
                before, skill, marker, arg = found
                assert ctx is not None
                interim = split_actions(split_sticker(split_mood(before)[0])[0])[0]
                interim = SILENT_MARK.sub("", interim).strip()
                if interim:
                    interim_event = self.ingest.record_self(
                        body=ctx.body, channel=ctx.channel, content=interim, ts=ctx.now
                    )
                    self.gateway.note_response(result, interim_event.id)
                    if on_interim is not None:
                        on_interim(interim)
                call, outcome = self.skills.run(skill, marker, arg, ctx)
                calls.append(call)
                if outcome.temporary:
                    temporary += outcome.view
                if outcome.temporary or skill.name in ("screen", "clipboard"):
                    # 临时图片、剪贴板文字都不进入持久 rollout，继续使用原来的完整本轮提示词。
                    session_key = None
                said = before + (f"【{marker}：{arg}】" if arg else f"【{marker}】")
                messages += [
                    ChatMessage("assistant", said),
                    ChatMessage(
                        "skill",
                        self.skills.feedback(skill, marker, outcome),
                        images=tuple(outcome.view),
                    ),
                ]
                session_bootstrap += messages[-2:]
                # 原生线程已保存它自己的回复与设定，只补交最新技能结果与本轮精简页脚。
                session_messages = (
                    [*messages[-1:], *turn_tail]
                    if result.raw.get("session_key") == session_key and session_key
                    else [*session_prefix_and_history, *messages[-2:], *turn_tail]
                )
        finally:
            for path in temporary:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)

    def deliver(
        self, body: str, channel: str, result: SkillResult, now: datetime | None = None
    ) -> list[Path]:
        """后台技能做完了：把图片存进图片库、记成她发的消息，返回要发出去的图片路径。"""
        if not result.ok:
            return []
        paths, digests = [], []
        animated = False
        for image in result.images:
            try:
                stored = self.media.save(image.read_bytes())
            except (OSError, media.MediaError) as e:
                log.warning("保存技能图片失败：%s", e)
                continue
            # 动图要发原图才会动；静态图发压缩过的，省流量
            gif = image.suffix.lower() == ".gif"
            animated = animated or gif
            paths.append(
                (self.media.original_for(stored.digest) or stored.path) if gif else stored.path
            )
            digests.append(stored.digest)
        if digests:
            what = "动图" if animated else "图"
            self.ingest.record_self(
                body=body,
                channel=channel,
                content=f"[我画的{what}{result.note}：{media.clean(result.text)}]",
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
            files = self._files_in(answer.text, sender, ctx)
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

    def _files_in(self, text: str, sender: SendFile | None, ctx: SkillContext) -> list[Path]:
        """她话里的【发文件：…】：这个场合能发、大小合适的才发。"""
        if sender is None:
            return []
        files = []
        for match in MARKER.finditer(text):
            if match.group(1) in sender.markers and match.group(2):
                try:
                    files.append(sender.resolve(match.group(2), ctx))
                except SkillError as e:
                    log.info("没发文件：%s", e)
        return list(dict.fromkeys(files))

    def _record_sent_images(
        self, body: str, channel: str, images: list[Path], now: datetime | None
    ) -> None:
        for path in images:
            name = path.name.split(".orig.")[0][:8] if ".orig." in path.name else path.name
            self.ingest.record_self(
                body=body, channel=channel, content=f"[我发的图：{name}]", ts=now,
                meta={"file": str(path)},
            )  # fmt: skip

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

    # ---- 约定与提醒 ----

    def remind_text(self, reminder: Reminder, now: datetime) -> tuple[str, Sticker | None]:
        """到点了：让她用自己的话提醒。这时还没发出去，发成功后再调 promise_sent 记下。

        模型不可用或她没写出来时，退回一句直白的话：提醒比措辞重要。
        """
        who = self._display(reminder.person_id)
        due = reminder.due or now
        late = now - due
        lateness = ""
        flexible = bool(reminder.repeat and reminder.repeat.flexible)
        if late > (timedelta(hours=12) if flexible else REMINDER_LATE):
            lateness = (
                f"这条本该在 {due.astimezone():%m月%d日 %H:%M} 提醒，现在已经迟了，"
                "先说明一下迟到了。"
            )
        history_note = ""
        if reminder.history:
            history_note = "（上次说过：" + reminder.history[-1] + "，这次换个自然的说法。）"
        note = (
            f"（到时间了。你和{who}之前约好提醒：{reminder.what}。{lateness}{history_note}"
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
        if actions.voice:  # 提醒与约定统一用文字发出，避免私聊误发语音
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
        self.promise_sent(
            reminder,
            PromiseOutcome(ok=True, text=text, sticker=sticker, summary=text[:80]),
            now,
        )

    def fulfill_promise(self, promise: Promise, now: datetime) -> PromiseOutcome:
        """执行一条到点的约定或提醒：口头提醒走快速通道，行动约定可调用受控技能完成产物。"""
        if promise.kind != "action":
            text, sticker = self.remind_text(promise, now)
            return PromiseOutcome(ok=True, text=text, sticker=sticker, summary=text[:80])
        return self._fulfill_action_promise(promise, now)

    def _fulfill_action_promise(self, promise: Promise, now: datetime) -> PromiseOutcome:
        who = self._display(promise.person_id)
        is_private = promise.channel.startswith("private:")
        person = self.people.get(promise.person_id)
        qq_accs = set(person.accounts.get("qq") or []) if person else set()
        is_owner = bool(qq_accs & set(self.config.qq.owner_accounts))
        ctx = SkillContext(
            body="qq",
            channel=promise.channel,
            person_id=promise.person_id,
            private=is_private,
            owner=is_owner,
            now=now,
            query=promise.what,
        )

        companion_self = self
        paint_notes: list[str] = []
        paint_failed = False

        class _SyncPromiseSkills(Skills):
            def run(
                self, skill: Skill, marker: str, arg: str, run_ctx: SkillContext
            ) -> tuple[SkillCall, SkillResult]:
                nonlocal paint_failed
                call, res = super().run(skill, marker, arg, run_ctx)
                if res.later is not None:
                    painted = res.later()
                    call.later = None
                    res.later = None
                    call.ok = painted.ok
                    res.ok = painted.ok
                    if painted.ok:
                        saved_imgs = companion_self._save_promise_images(painted)
                        call.images = saved_imgs
                        res.images = saved_imgs
                        if painted.note:
                            paint_notes.append(painted.note)
                        res.text = (
                            f"已经画好并处理完毕{painted.note}：{painted.text[:220]}。"
                            "图片已就绪，不需要再重复调用【画】。"
                        )
                    else:
                        paint_failed = True
                        res.text = painted.text
                elif skill.name == "image" and not res.ok:
                    paint_failed = True
                return call, res

        action_skills = _SyncPromiseSkills(
            [s for s in self.skills.all if s.name in PROMISE_SAFE_SKILLS],
            self.config.skills,
            self.config.data_dir / "skills.jsonl",
        )
        skill_prompt, skill_notes = action_skills.prompt_parts(ctx)
        history_block = (
            "过往履约记录（最近几次已经做过的，注意保持连贯、避免重复同一内容）：\n"
            + "\n".join(f"- {h}" for h in promise.history[-10:])
            if promise.history
            else "过往履约记录：（这是第一次履约）"
        )
        delivery_hint = (
            "这个约定设为静默完成：把产物存好后，最后只写一句简短记录说明今天做了什么，不会发消息打扰对方。"
            if promise.notify == "quiet"
            else (
                f"把事情真正做完并保存好后，用你自己的语气自然地告诉{who}一声"
                "（一两句话，像记着约定主动分享，不要像机器汇报）。"
            )
        )
        note = (
            f"（到了你和{who}约定的执行时间：{promise.what}\n"
            f"{history_block}\n"
            "请先调用需要的技能真正把事情完成（例如：要求每次不重样时先【看目录】或【读文件】核对已有内容；"
            "画画并存入文件夹用【画：具体画面描述｜存到：文件夹/文件名】；"
            "需要生成整本 PDF 图册可在存好图后用【做图册：文件夹｜名字】；"
            f"写文字存档用【写文件：路径】）。\n{delivery_hint}）"
        )
        cfg = self.config.stickers
        emotions = (
            self.stickers.emotions()
            if cfg.enabled and len(self.stickers) >= cfg.min_library and promise.notify == "always"
            else []
        )
        bundle = self.context.build(
            person_id=promise.person_id,
            body="qq",
            channel=promise.channel,
            query=promise.what,
            now=now,
            sticker_emotions=emotions,
            reply_mode="promise_action",
            extra_sections=[skill_prompt] if skill_prompt else None,
            dynamic_sections=skill_notes,
            initiative=note,
            show_image_refs=action_skills.can_use("看图", ctx),
        )

        messages = list(bundle.messages)
        calls: list[SkillCall] = []
        bridge = (
            NativeSkills(action_skills, ctx, lambda call, _out: calls.append(call))
            if self.config.models.subscription.native_tools
            else None
        )
        result: ChatResult | None = None
        try:
            for _ in range(8):
                result = self.gateway.chat(
                    messages,
                    purpose="promise",
                    tools=bridge.tools if bridge else None,
                )
                found = action_skills.find(result.text, ctx)
                if found is None:
                    break
                before, skill, marker, arg = found
                call, outcome = action_skills.run(skill, marker, arg, ctx)
                calls.append(call)
                said = before + (f"【{marker}：{arg}】" if arg else f"【{marker}】")
                messages += [
                    ChatMessage("assistant", said),
                    ChatMessage(
                        "skill",
                        action_skills.feedback(skill, marker, outcome),
                        images=tuple(outcome.view),
                    ),
                ]
        except ModelError as e:
            log.warning("执行约定 %d 失败：%s", promise.id, e)
            if not any(c.ok for c in calls):
                return PromiseOutcome(ok=False, defer_minutes=15)

        images = [p for c in calls for p in c.images]
        files = [p for c in calls for p in c.files]
        wrote_ok = any(c.ok and c.skill in {"write_files", "pictures", "image"} for c in calls)
        if paint_failed and not images and not files and not wrote_ok:
            log.info("约定 %d 因画图暂未成功推迟重试", promise.id)
            return PromiseOutcome(ok=False, defer_minutes=20)
        if result is None:
            return PromiseOutcome(ok=False, defer_minutes=15)

        raw_text = self._take_mood(
            action_skills.strip_markers(result.text), promise.channel, promise.person_id, now
        )
        raw_text = IMAGE_REF_NOTE.sub("", raw_text)
        raw_text, wanted = split_sticker(raw_text)
        raw_text, actions = split_actions(raw_text)
        if actions.voice:
            raw_text = f"{raw_text} {actions.voice}".strip()
        text = SILENT_MARK.sub("", raw_text).strip()
        if promise.notify == "always" and not text:
            text = f"按我们约好的，今天这件事做好啦：{promise.what}"

        step_bits = [f"{c.marker}({c.arg[:30]})" for c in calls if c.ok]
        summary_parts = [*step_bits, *paint_notes]
        if text:
            summary_parts.append(text[:60])
        summary = "｜".join(summary_parts) or promise.what[:60]

        if promise.notify == "quiet":
            return PromiseOutcome(ok=True, summary=summary)
        sticker = (
            self._choose_sticker(promise.channel, wanted, now) if wanted and emotions else None
        )
        return PromiseOutcome(
            ok=True,
            text=text,
            sticker=sticker,
            images=images,
            files=files,
            summary=summary,
        )

    def _save_promise_images(self, result: SkillResult) -> list[Path]:
        paths: list[Path] = []
        for image in result.images:
            try:
                stored = self.media.save(image.read_bytes())
            except (OSError, media.MediaError) as e:
                log.warning("保存履约图片失败：%s", e)
                continue
            gif = image.suffix.lower() == ".gif"
            paths.append(
                (self.media.original_for(stored.digest) or stored.path) if gif else stored.path
            )
        return paths

    def promise_sent(self, promise: Promise, outcome: PromiseOutcome, now: datetime) -> None:
        """约定已履行（并发出通知或静默落盘）：记录事件、生活轨迹并推进到下一周期。"""
        if promise.notify == "always" and outcome.text:
            self.ingest.record_self(
                body="qq",
                channel=promise.channel,
                content=outcome.text,
                ts=now,
                meta={"reminder": promise.id, "promise_kind": promise.kind},
            )
        if outcome.sticker is not None:
            self._record_sticker("qq", promise.channel, outcome.sticker, now)
        if outcome.images:
            self._record_sent_images("qq", promise.channel, outcome.images, now)
        if outcome.files:
            self._record_files("qq", promise.channel, outcome.files, now)
        if promise.kind == "action":
            self.life.days.append(
                now,
                "activities",
                {
                    "ts": now.isoformat(),
                    "kind": "promise",
                    "title": f"履约：{promise.what[:30]}",
                    "summary": outcome.summary[:80],
                },
            )
        self.reminders.finish(promise.id, now, summary=outcome.summary)

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
                codex_home=sub.codex_home,
                overrides=talk_only_overrides(image=True, web=True),
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
        # 旧上下文/压缩摘要可能仍带被遗忘内容，不再复用这些线程。
        self.gateway.rotate_chat_sessions("forget")
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
            original = self.media.original_for(stored.digest)
            known = self.stickers.get(stored.digest)
            if known is not None:  # 收藏过的表情不用再看一遍
                self.stickers.add(
                    stored.path,
                    stored.digest,
                    known.description,
                    known.emotion,
                    person_id,
                    original=original,
                )
                return known.description, stored.digest
            description, emotion, usable = media.describe_sticker(self.gateway, stored.path)
            log.info("看表情包（%s，%s）：%s", emotion, "收藏" if usable else "不收藏", description)
            if usable:
                self.stickers.add(
                    stored.path, stored.digest, description, emotion, person_id, original=original
                )
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

    def rotate_chat_sessions(self, reason: str) -> int:
        """仅在原始事件已整理后由维护/停止流程调用；不影响后台任务线程。"""
        n = self.gateway.rotate_chat_sessions(reason)
        if n:
            log.info("聊天线程已轮换（%s）：%d 个，下次聊天按需创建", reason, n)
        return n

    def extract_pending(
        self, *, channel: str | None = None, force: bool = False, now: datetime | None = None
    ) -> list[Extraction]:
        """把已经结束的会话抽取成记忆。`force` 时不等空闲，立刻整理（如退出命令行时）。"""
        now = now or datetime.now(UTC)
        idle = timedelta(minutes=self.config.retrieval.session_idle_minutes)
        by_channel: dict[str, list[Event]] = {}
        from companion.trpg.policy import is_fiction, registry

        for e in self.log.iter_events():
            if is_fiction(e, registry(self.config.data_dir)):
                continue  # 团状态不抽成现实人物事实；公开团经历仍可跨聊天按来源回忆。
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
                # 日切会强制整理仍活跃的会话；按事件边界分批，失败可从已提交游标续做。
                for part in self._extraction_batches(session):
                    extraction = self.extractor.extract(part)
                    self._store(extraction)
                    self._set_cursor(ch, part[-1])
                    results.append(extraction)
        if results:
            self.sync_vectors()
            commit_memory(
                self.layout,
                self.name,
                f"整理记忆：{len(results)} 段会话（{now.astimezone():%m-%d %H:%M}）",
            )
        return results

    @staticmethod
    def _extraction_batches(events: list[Event]) -> list[list[Event]]:
        batches: list[list[Event]] = []
        current: list[Event] = []
        chars = 0
        for event in events:
            size = len(event.content)
            if current and (chars + size > 24000 or len(current) >= 60):
                batches.append(current)
                current, chars = [], 0
            current.append(event)
            chars += size
        if current:
            batches.append(current)
        return batches

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
        # 人物事实或经历更新后，持久会话会在下一轮通过 state@person:<id> 增量推送最新事实，
        # 不再在日间空闲抽取时清空整天的聊天线程。

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
