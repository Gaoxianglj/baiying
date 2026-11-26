"""自主活动与主动找主人绑定、早晚安固定发送、权重优先度、以及人物基础图+衣着联动设定图测试。"""

from __future__ import annotations

import collections
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.sleep import Sleeper
from companion.life.activities import ActivityPrep, ActivityRunner
from companion.life.appearance import AppearanceStore
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import RateLimits, RateWindow
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatResult
from companion.skills.base import SkillContext
from companion.skills.image import ImageGen
from companion.web import settings as web_settings
from tests.fakes import FakeEmbedder

OWNER = "2000"
CHANNEL = f"private:{OWNER}"


def local(*args: int) -> datetime:
    return datetime(*args).astimezone()


NOW = local(2026, 11, 26, 15, 0)


def _limits(used: float = 5.0, minutes_left: float = 60.0, now: datetime = NOW) -> RateLimits:
    return RateLimits(
        (
            RateWindow(used, 300, now + timedelta(minutes=minutes_left)),
            RateWindow(10.0, 10080, now + timedelta(days=3)),
        )
    )


class _FakeSub:
    name = "subscription"

    def __init__(self, reply: str = "好呀") -> None:
        self.reply = reply
        self.limits = _limits()
        self.last_rate_limits = None

    def read_rate_limits(self) -> RateLimits:
        self.last_rate_limits = self.limits
        return self.limits

    def chat(self, messages: Any, *, json_schema: Any = None, max_tokens: Any = None) -> ChatResult:
        return ChatResult(text=self.reply, backend="subscription", model="fake")

    def close(self) -> None:
        pass


class _FakeLifeBackend:
    def __init__(self, tmp_path: Path, choice: dict[str, Any]) -> None:
        self.tmp_path = tmp_path
        self.choice = choice
        self.prompts: list[str] = []
        self.gen_calls: list[tuple[str, list[Path]]] = []
        self._seq = 0

    def chat(self, messages: Any, *, json_schema: Any = None) -> ChatResult:
        system = messages[0].content
        self.prompts.append(system)
        if "空闲时间" in system:
            return ChatResult(json.dumps(self.choice, ensure_ascii=False), "life", "fake")
        if "现代短诗" in system:
            data = {
                "title": "檐下雪",
                "text": "檐角停着一点白，\n风过时轻了半寸。\n书页还没翻完，\n窗已经亮了。",
                "sources": [],
                "share": False,
                "mood": "",
            }
        elif "随笔或微片段" in system:
            data = {
                "title": "窗台",
                "text": "午后光线偏过来，落在杯沿上。",
                "sources": [],
                "share": False,
                "mood": "",
            }
        else:
            data = {
                "title": "查了蜡梅",
                "text": "蜡梅不是梅花。",
                "sources": ["https://example.org"],
                "share": False,
                "mood": "",
            }
        return ChatResult(json.dumps(data, ensure_ascii=False), "life", "fake")

    def generate_image(self, prompt: str, references: Any = ()) -> tuple[Path, str]:
        self._seq += 1
        out = self.tmp_path / f"gen_{self._seq}.png"
        Image.new("RGB", (16, 16), "white").save(out)
        refs = list(references)
        self.gen_calls.append((prompt, refs))
        return out, f"generated-{self._seq}"

    def close(self) -> None:
        pass


def _make_companion(tmp_path: Path) -> tuple[Companion, _FakeLifeBackend]:
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_path, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)
    sub = _FakeSub()
    comp = Companion(cfg, gateway=ModelGateway(subscription=sub), embedder=FakeEmbedder())
    backend = _FakeLifeBackend(tmp_path, {"kind": "draw", "topic": "窗外雪景", "why": "好看"})
    comp.activities = ActivityRunner(
        lambda: backend, appearance=comp.appearance, rng=random.Random(42)
    )
    return comp, backend


# ---- 1. 绑定做事与主动发消息 + 早安晚安固定发送 ----


def test_greetings_are_fixed_and_independent_of_unanswered_shares(tmp_path: Path):
    comp, _ = _make_companion(tmp_path)
    try:
        owner_id = comp.people.ensure("qq", OWNER, "主人")
        policy = comp.life.owner
        # 今天主动分享次数已用满 4 次且主人一条都没回
        for i in range(4):
            policy.record(local(2026, 11, 26, 9 + i, 0), "share", f"分享 {i}")
        assert policy.can_share(owner_id, CHANNEL, local(2026, 11, 26, 16, 0))[0] is False

        # 晚安依然按点固定发送，不受主动分享次数和主人未回影响
        night_t = policy.greeting_time(NOW, "night", "23:30") + timedelta(minutes=1)
        assert policy.greeting_due(owner_id, CHANNEL, night_t) == "night"

        # 早安也不占用分享次数，且早安发送后不触发 120 分钟分享冷却
        day2 = local(2026, 11, 27, 9, 0)
        morning_t = policy.greeting_time(day2, "morning", "08:30") + timedelta(minutes=1)
        policy.record(morning_t, "morning", "早呀")
        # 昨天未回的 share 仍会被检查；主人回过昨天那条后，早安刚发完 10 分钟也能分享
        comp.observe(
            body="qq",
            channel=CHANNEL,
            account=OWNER,
            text="早",
            now=morning_t - timedelta(minutes=70),
            display_name="主人",
        )
        assert policy.can_share(owner_id, CHANNEL, morning_t + timedelta(minutes=10)) == (True, "")
    finally:
        comp.close()


def test_prepare_activity_bound_to_owner_shareable(tmp_path: Path):
    comp, _ = _make_companion(tmp_path)
    try:
        owner_id = comp.people.ensure("qq", OWNER, "主人")
        assert comp.config.life.activities.require_owner_shareable is True
        # 正常白天且能分享时允许做事
        prep = comp.prepare_activity(NOW, owner_id=owner_id, owner_channel=CHANNEL)
        assert prep is not None and prep.share_required is True

        # 已经发过一条主动分享且主人还没回 -> 绑定机制拦住自主活动，不浪费额度
        comp.life.owner.record(NOW - timedelta(hours=3), "share", "刚画的图")
        assert comp.prepare_activity(NOW, owner_id=owner_id, owner_channel=CHANNEL) is None

        # 安静时段也拦住
        quiet_t = local(2026, 11, 26, 3, 0)
        comp.gateway.subscription.limits = _limits(now=quiet_t)  # type: ignore[union-attr]
        comp.life._limits = None
        assert comp.prepare_activity(quiet_t, owner_id=owner_id, owner_channel=CHANNEL) is None
    finally:
        comp.close()


# ---- 2. 活动拆分权重与控制台设置 ----


def test_activity_weights_prioritize_draw_then_poem_then_essay(tmp_path: Path):
    runner = ActivityRunner(None, rng=random.Random(12345))
    weights = {"draw": 5, "poem": 3, "explore": 2, "chat_owner": 2, "essay": 1}
    counts = collections.Counter(runner._pick_kind(weights, []) for _ in range(3000))
    assert counts["draw"] > counts["poem"] > counts["explore"] > counts["essay"]

    # 把散文与小片段设为 0 时绝不选到
    no_essay = {**weights, "essay": 0}
    assert all(runner._pick_kind(no_essay, []) != "essay" for _ in range(200))


def test_poem_and_essay_use_distinct_prompts_and_save_writings(tmp_path: Path):
    comp, backend = _make_companion(tmp_path)
    try:
        backend.choice = {
            "kind": "poem",
            "topic": "檐下雪",
            "why": "起风了",
            "includes_self": False,
        }
        prep_poem = ActivityPrep(
            "白璎",
            "主人",
            ["poem"],
            "【你的兴趣】\n写诗",
            NOW,
            weights={"poem": 5},
            share_required=True,
        )
        res_poem = comp.activities.perform(prep_poem)
        assert res_poem is not None and res_poem.kind == "poem" and res_poem.share is True
        assert any("现代短诗" in p for p in backend.prompts)
        comp.finish_activity(res_poem, NOW)

        backend.choice = {"kind": "essay", "topic": "窗台", "why": "发呆", "includes_self": False}
        prep_essay = ActivityPrep(
            "白璎",
            "主人",
            ["essay"],
            "【你的兴趣】\n随笔",
            NOW + timedelta(minutes=30),
            weights={"essay": 5},
            share_required=True,
        )
        res_essay = comp.activities.perform(prep_essay)
        assert res_essay is not None and res_essay.kind == "essay" and res_essay.share is True
        assert any("随笔或微片段" in p for p in backend.prompts)
        comp.finish_activity(res_essay, NOW + timedelta(minutes=30))

        acts = comp.life.days.entries(NOW, "activities")
        assert [a["kind"] for a in acts[-2:]] == ["poem", "essay"]
        assert len(list((comp.layout.self_dir / "writings").glob("*.md"))) == 2
    finally:
        comp.close()


def test_console_settings_expose_activity_weights_and_binding(tmp_path: Path):
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    fields = {f["path"]: f for f in web_settings.describe(tmp_path)}
    for key in (
        "life.activities.require_owner_shareable",
        "life.activities.weight_draw",
        "life.activities.weight_poem",
        "life.activities.weight_essay",
        "life.activities.weight_explore",
        "life.activities.weight_chat_owner",
    ):
        assert key in fields
    assert fields["life.activities.weight_draw"]["value"] == 5
    assert fields["life.activities.weight_poem"]["value"] == 3
    assert fields["life.activities.weight_essay"]["value"] == 1
    web_settings.save(
        tmp_path, {"life.activities.weight_essay": 0, "life.activities.weight_draw": 8}
    )
    reloaded = load_config(tmp_path, environ={})
    assert reloaded.life.activities.weight_essay == 0
    assert reloaded.life.activities.weight_draw == 8


# ---- 3 & 4. 画画主题偏好与人物基础图 + 衣着联动设定图 ----


def test_appearance_adopt_and_auto_refresh_on_outfit_change(tmp_path: Path):
    comp, backend = _make_companion(tmp_path)
    try:
        app: AppearanceStore = comp.appearance
        # 放入两张图前：不报错，不触发重画
        assert app.needs_outfit_refresh() is False
        assert app.self_draw_reference(backend, now=NOW) is None

        # 主人确认后放入 base.png 和 outfit.png（不手写 state.json）-> 立即可用且不重复生成
        app.dir.mkdir(parents=True, exist_ok=True)
        base_img = app.dir / "base.png"
        outfit_img = app.dir / "outfit.png"
        Image.new("RGB", (20, 20), "white").save(base_img)
        Image.new("RGB", (20, 20), "silver").save(outfit_img)
        assert app.needs_outfit_refresh() is False
        assert app.self_draw_reference(backend, now=NOW) == outfit_img
        assert backend.gen_calls == []
        assert app.state_path.exists()

        # 自主画风景：不带人物设定图；且提示词含风景/植物/画自己优先引导
        backend.choice = {
            "kind": "draw",
            "topic": "冬日窗外的银杏树",
            "why": "安静",
            "includes_self": False,
        }
        prep_scene = ActivityPrep(
            "白璎",
            "主人",
            ["draw"],
            "【你的兴趣】\n画画",
            NOW,
            weights={"draw": 5},
            outfit_text=app.current_outfit_text(),
        )
        res_scene = comp.activities.perform(prep_scene)
        assert res_scene is not None and res_scene.kind == "draw"
        assert any("风景" in p and "植物" in p and "画你自己" in p for p in backend.prompts)
        assert backend.gen_calls[-1][1] == []  # 风景不附带人物设定图

        # 自主画她自己（衣着未变）：直接以现有 outfit.png 作为参考图
        backend.choice = {
            "kind": "draw",
            "topic": "白璎坐在窗边翻书",
            "why": "想画自己",
            "includes_self": True,
        }
        res_self = comp.activities.perform(prep_scene)
        assert res_self is not None and res_self.kind == "draw"
        assert backend.gen_calls[-1][1] == [outfit_img]

        # 现在衣服发生变化（修改 preferences.md 里的“穿搭与随身物”）
        pref_path = comp.layout.self_dir / "preferences.md"
        pref_path.write_text(
            "# 白璎 的喜好与习惯\n\n## 穿搭与随身物\n\n- 换上了浅青灰色的羊绒长外套配月白围巾。\n",
            encoding="utf-8",
        )
        assert app.needs_outfit_refresh() is True

        # 下一次画自己时：先自动用 base.png + 新衣服描述重生成 outfit.png 并归档旧图，再用新图画场景
        prev_calls = len(backend.gen_calls)
        prep_self_new = ActivityPrep(
            "白璎",
            "主人",
            ["draw"],
            "【你的兴趣】\n画画",
            NOW + timedelta(hours=1),
            weights={"draw": 5},
            outfit_text=app.current_outfit_text(),
        )
        comp.activities.perform(prep_self_new)
        new_calls = backend.gen_calls[prev_calls:]
        assert len(new_calls) == 2
        # 第 1 次生成：用 base.png + 新服装描述生成新服装设定图
        assert "羊绒长外套" in new_calls[0][0]
        assert new_calls[0][1] == [base_img]
        # 第 2 次生成：用刷新后的 outfit.png 画日常场景
        assert new_calls[1][1] == [outfit_img]
        assert app.needs_outfit_refresh() is False
        assert len(list(app.history_dir.glob("outfit-*.png"))) == 1
    finally:
        comp.close()


def test_image_skill_supports_self_reference_sheet(tmp_path: Path):
    comp, backend = _make_companion(tmp_path)
    try:
        app = comp.appearance
        app.dir.mkdir(parents=True, exist_ok=True)
        base_img = app.dir / "base.png"
        outfit_img = app.dir / "outfit.png"
        Image.new("RGB", (20, 20), "white").save(base_img)
        Image.new("RGB", (20, 20), "silver").save(outfit_img)

        skill = ImageGen(
            comp.config.skills.image,
            lambda: backend,
            tmp_path / "quota.json",
            appearance=app,
        )
        ctx = SkillContext("qq", CHANNEL, "p1", True, True)
        assert "设定图" in (skill.context(ctx) or "")
        res = skill.run("画", "雪天里在窗边捧着热茶｜参考：设定图", ctx)
        assert res.later is not None
        out = res.later()
        assert out.ok is True
        assert backend.gen_calls[-1][1] == [outfit_img]
    finally:
        comp.close()


def test_sleeper_triggers_outfit_refresh_when_preferences_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    comp, backend = _make_companion(tmp_path)
    try:
        app = comp.appearance
        app.dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20), "white").save(app.dir / "base.png")
        Image.new("RGB", (20, 20), "silver").save(app.dir / "outfit.png")
        app.adopt_existing_if_untracked(now=NOW)

        sleeper = Sleeper(comp)
        monkeypatch.setattr(sleeper, "_reflect_self", lambda since: False)
        monkeypatch.setattr(sleeper, "_reflect_style", lambda since: False)
        monkeypatch.setattr(sleeper, "_reflect_people_weekly", lambda since, errors=None: [])

        def fake_reflect_prefs(_since: datetime) -> bool:
            (comp.layout.self_dir / "preferences.md").write_text(
                "# 白璎 的喜好与习惯\n\n## 穿搭与随身物\n\n- 冬天添了件米白毛呢斗篷。\n",
                encoding="utf-8",
            )
            return True

        monkeypatch.setattr(sleeper, "_reflect_preferences", fake_reflect_prefs)
        sleeper.run(now=NOW)
        assert len(backend.gen_calls) == 1
        assert "米白毛呢斗篷" in backend.gen_calls[0][0]
        assert backend.gen_calls[0][1] == [app.dir / "base.png"]
        assert app.needs_outfit_refresh() is False
    finally:
        comp.close()
