import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.core.sleep import LAST_SLEEP, Sleeper
from companion.memory import MemoryLayout, init_memory
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.gateway import ModelGateway
from companion.models.openai_compat import ChatMessage, ModelError
from companion.models.sessions import ChatSessions
from companion.skills import SkillContext
from companion.skills.desktop import ReadClipboard
from tests.fakes import FakeEmbedder
from tests.test_models import deepseek

FAKE = str(Path(__file__).with_name("fake_codex_app_server.py"))
T0 = datetime(2026, 11, 10, 12, tzinfo=UTC)


def backend(folder, monkeypatch, **env):
    folder.mkdir(exist_ok=True)
    for k, v in {"FAKE_STATE_PATH": str(folder / "native.json"),
                 "FAKE_REQUEST_LOG": str(folder / "requests.jsonl"), **env}.items():
        monkeypatch.setenv(k, str(v))
    return CodexAppServerBackend(
        command=[sys.executable, FAKE], workdir=folder, timeout=5,
        sessions=ChatSessions(folder / "sessions.sqlite"),
    )


def requests(folder, method):
    return [r for line in (folder / "requests.jsonl").read_text("utf-8").splitlines()
            if (r := json.loads(line)).get("method") == method]


def text_of(request):
    return request["params"]["input"][0]["text"]


def messages(text="第一条", eid="e1"):
    return [ChatMessage("system", "你是白璎"), ChatMessage("user", text, event_id=eid)]


def test_reuses_thread_deduplicates_events_but_keeps_identical_new_text(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    try:
        first = b.chat(messages(), session_key="qq/private:1")
        b.note_response(first, "r1")
        second = b.chat([*messages(), ChatMessage("assistant", "你好呀", event_id="r1"),
                         ChatMessage("user", "第一条", event_id="e2")],
                        session_key="qq/private:1")
        assert first.raw["thread_id"] == second.raw["thread_id"]
        assert len(requests(tmp_path, "thread/start")) == 1
        assert text_of(requests(tmp_path, "turn/start")[1]).count("第一条") == 1
        assert "你之前说" not in text_of(requests(tmp_path, "turn/start")[1])
        assert requests(tmp_path, "thread/start")[0]["params"]["ephemeral"] is False
    finally:
        b.close()


def test_separate_chats_and_auxiliary_json_calls_never_share_thread(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    try:
        a = b.chat(messages(), session_key="qq/group:1")
        b2 = b.chat(messages(), session_key="qq/private:1")
        b.chat(messages("只抽取 JSON"), json_schema={"type": "object"}, session_key="qq/group:1")
        again = b.chat(messages("继续", "e2"), session_key="qq/group:1")
        assert a.raw["thread_id"] != b2.raw["thread_id"]
        assert again.raw["thread_id"] == a.raw["thread_id"]
        starts = requests(tmp_path, "thread/start")
        assert [r["params"]["ephemeral"] for r in starts] == [False, False, True]
    finally:
        b.close()


def test_process_restart_resumes_saved_thread_and_cursor(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    a = b.chat(messages(), session_key="qq/private:1")
    b.close()
    b.sessions.close()
    fresh = backend(tmp_path, monkeypatch)
    try:
        c = fresh.chat([*messages(), ChatMessage("user", "重启后继续", event_id="e2")],
                       session_key="qq/private:1")
        assert c.raw["thread_id"] == a.raw["thread_id"]
        assert len(requests(tmp_path, "thread/start")) == 1
        assert len(requests(tmp_path, "thread/resume")) == 1
        assert "第一条" not in text_of(requests(tmp_path, "turn/start")[-1])
    finally:
        fresh.close()


def test_missing_rollout_rebuilds_from_bootstrap_without_losing_request(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    b.chat(messages(), session_key="qq/group:1")
    b.close()
    (tmp_path / "native.json").write_text("{}", "utf-8")
    try:
        result = b.chat(messages("继续", "e2"), session_key="qq/group:1",
                        session_messages=messages("只增量", "e2"),
                        session_bootstrap=[*messages(), ChatMessage("user", "继续", event_id="e2")])
        assert result.text
        assert len(requests(tmp_path, "thread/start")) == 2
        assert "第一条" in text_of(requests(tmp_path, "turn/start")[-1])
    finally:
        b.close()


def test_failed_turn_and_compaction_recovery(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch, FAKE_FAIL="usage_limit")
    with pytest.raises(ModelError):
        b.chat(messages(), session_key="qq/group:1")
    assert b.sessions.get("qq/group:1")[2] is True
    monkeypatch.setenv("FAKE_FAIL", "")
    monkeypatch.setenv("FAKE_COMPACT", "1")
    try:
        result = b.chat(messages("重试", "e2"), session_key="qq/group:1")
        assert result.text == "你好呀"
        assert len(requests(tmp_path, "thread/start")) == 2
        assert not b.sessions.get("qq/group:1")[2]
    finally:
        b.close()


def companion(folder, b):
    (folder / "config.toml").write_text("", "utf-8")
    cfg = load_config(folder, environ={})
    init_memory(MemoryLayout(cfg.memory_dir), "白璎", git=False)
    return Companion(cfg, gateway=ModelGateway(subscription=b, fallback=deepseek()),
                     embedder=FakeEmbedder(), skills=[])


def say(c, text, n, channel="private:1"):
    return c.reply(body="qq", channel=channel, account="one", display_name="同一个人",
                   text=text, now=T0 + timedelta(seconds=n))


def test_persona_and_tool_prefix_sent_once_but_runtime_notes_refresh(tmp_path, monkeypatch):
    from companion.skills.base import Skill

    class ChangingNote(Skill):
        name = "note"
        markers = {"测试": "固定技能说明-unique"}
        note = "实时状态-one"

        def context(self, ctx):
            return self.note

    folder = tmp_path / "codex"
    with companion(tmp_path, backend(folder, monkeypatch)) as c:
        skill = ChangingNote()
        c.skills.all.append(skill)
        say(c, "第一轮", 0)
        skill.note = "实时状态-two"
        say(c, "第二轮", 65)
        turns = requests(folder, "turn/start")
        assert "固定技能说明-unique" in text_of(turns[0])
        assert "固定技能说明-unique" not in text_of(turns[1])
        assert "实时状态-two" in text_of(turns[1])
        (c.layout.self_dir / "style.md").write_text("新表达习惯-unique", "utf-8")
        say(c, "第三轮", 130)
        assert "新表达习惯-unique" in text_of(requests(folder, "turn/start")[-1])


def test_fallback_gap_longer_than_recent_window_is_synchronized(tmp_path, monkeypatch):
    folder = tmp_path / "codex"
    b = backend(folder, monkeypatch)
    with companion(tmp_path, b) as c:
        first = say(c, "起点", 0)
        c.gateway.prefer = "fallback"
        for n in range(35):
            say(c, f"兜底第{n}条", n + 1)
        c.gateway.prefer = "subscription"
        last = say(c, "现在接回来", 40)
        assert last.result.raw["thread_id"] == first.result.raw["thread_id"]
        payload = text_of(requests(folder, "turn/start")[-1])
        assert "兜底第0条" in payload and "兜底第34条" in payload
        assert "起点" not in payload


def test_recent_memory_is_shared_both_directions_without_extraction(tmp_path, monkeypatch):
    with companion(tmp_path, backend(tmp_path / "codex", monkeypatch)) as c:
        say(c, "我的幸运数字是 78321", 0)
        say(c, "我刚才的幸运数字是多少？", 1, "group:2")
        group_prompt = c.context.build(
            person_id=c.people.resolve("qq", "one"), body="qq", channel="group:2",
            query="幸运数字", now=T0 + timedelta(seconds=2),
        )
        assert "78321" in group_prompt.system_prompt
        say(c, "群里决定活动代号叫青松", 3, "group:2")
        private_prompt = c.context.build(
            person_id=c.people.resolve("qq", "one"), body="qq", channel="private:1",
            query="活动代号", now=T0 + timedelta(seconds=4),
        )
        assert "青松" in private_prompt.system_prompt
        assert c.index.episodes_created_since(T0) == []


def test_clipboard_tool_result_is_only_sent_to_ephemeral_thread(tmp_path, monkeypatch):
    b = backend(tmp_path / "codex", monkeypatch,
                FAKE_REPLIES=json.dumps(["【读剪贴板】", "看完了"], ensure_ascii=False))
    with companion(tmp_path, b) as c:
        c.config.skills.clipboard.enabled = True
        from companion.skills.access import FileAccess

        c.skills.all.append(ReadClipboard(c.config.skills.clipboard,
                                          FileAccess(c.config.skills.files, tmp_path,
                                                     tmp_path / "ws", lambda: []),
                                          tmp_path / "screen", read=lambda: "临时密文ONLY-HERE"))
        event = c.observe(body="qq", channel="private:1", account="one", text="看下剪贴板", now=T0)
        ctx = SkillContext("qq", "private:1", event.actor.person_id,
                           private=True, owner=True, now=T0)
        c.respond(event, skill_ctx=ctx, now=T0)
        starts = requests(tmp_path / "codex", "thread/start")
        assert [r["params"]["ephemeral"] for r in starts] == [False, True]
        turns = requests(tmp_path / "codex", "turn/start")
        assert "ONLY-HERE" not in text_of(turns[0]) and "ONLY-HERE" in text_of(turns[1])


def test_sleep_force_extracts_before_rotation_and_failure_keeps_thread(tmp_path, monkeypatch):
    b = backend(tmp_path / "codex", monkeypatch)
    with companion(tmp_path, b) as c:
        original = say(c, "记住这轮", 0).result.raw["thread_id"]
        calls = []

        def broken(**kw):
            calls.append(kw)
            raise ModelError("测试抽取失败")

        monkeypatch.setattr(c, "extract_pending", broken)
        s = Sleeper(c)
        report = s.run(now=T0 + timedelta(seconds=1))
        assert calls[0]["force"] is True
        assert report.errors and c.index.get_meta(LAST_SLEEP) is None
        assert b.sessions.get("qq/private:1")[0] == original
        monkeypatch.setattr(c, "extract_pending", lambda **kw: [])
        monkeypatch.setattr(c.life, "ensure_plan", lambda now: None)
        report = s.run(now=T0 + timedelta(seconds=2))
        assert report.threads_rotated == 1 and b.sessions.get("qq/private:1") is None
        assert c.index.get_meta(LAST_SLEEP) is not None
        assert say(c, "新的一轮", 3).result.raw["thread_id"] != original


def test_forgetting_invalidates_both_chats_native_context(tmp_path, monkeypatch):
    b = backend(tmp_path / "codex", monkeypatch)
    with companion(tmp_path, b) as c:
        say(c, "待忘记的普通信息", 0)
        say(c, "群里接上", 1, "group:2")
        source = next(e for e in c.log.iter_events() if e.actor.person_id != "self")
        fact = c.remember(source, "待忘记的普通信息", now=T0)
        c.forget([("fact", fact.id)])
        assert b.sessions.get("qq/private:1") is None
        assert b.sessions.get("qq/group:2") is None


def test_rotation_is_shared_across_backend_instances(tmp_path):
    first = ChatSessions(tmp_path / "sessions.sqlite")
    second = ChatSessions(tmp_path / "sessions.sqlite")
    first.put("qq/private:1", "thread-1", "policy")
    first.complete("qq/private:1", "thread-1", ["event-1"])
    assert not second.unseen("qq/private:1", "event-1")
    assert second.retire("sleep") == ["thread-1"]
    assert first.get("qq/private:1") is None
    first.close()
    second.close()


def test_skill_chain_stays_in_thread_and_returns_only_new_tool_result(tmp_path, monkeypatch):
    from tests.test_skills import Echo

    b = backend(tmp_path / "codex", monkeypatch,
                FAKE_REPLIES=json.dumps(["【搜索：松树】", "松树结果已收到"], ensure_ascii=False))
    with companion(tmp_path, b) as c:
        echo = Echo()
        c.skills.all.append(echo)
        r = say(c, "搜索松树", 0)
        assert r.text == "松树结果已收到" and echo.calls == ["松树"]
        turns = requests(tmp_path / "codex", "turn/start")
        assert turns[0]["params"]["threadId"] == turns[1]["params"]["threadId"]
        assert "关于松树的结果" in text_of(turns[1])
        assert "<你之前说>" not in text_of(turns[1])


def test_partial_extraction_failure_retries_only_uncommitted_batches(tmp_path, monkeypatch):
    from companion.core.extract import Extraction

    with companion(tmp_path, backend(tmp_path / "codex", monkeypatch)) as c:
        for n in range(4):
            c.observe(body="qq", channel="private:1", account="one", text=str(n) * 13000,
                      now=T0 + timedelta(seconds=n))
        seen = []

        def extract(events):
            seen.append(events[0].content[0])
            if len(seen) == 2:
                raise ModelError("第二批抽取失败")
            return Extraction(None)

        monkeypatch.setattr(c.extractor, "extract", extract)
        with pytest.raises(ModelError):
            c.extract_pending(force=True, now=T0 + timedelta(minutes=1))
        c.extract_pending(force=True, now=T0 + timedelta(minutes=1))
        assert seen == ["0", "1", "1", "2", "3"]


def test_reply_mode_switch_and_steady_turns_do_not_resend_persona_or_facts(tmp_path, monkeypatch):
    folder = tmp_path / "codex"
    b = backend(folder, monkeypatch)
    with companion(tmp_path, b) as c:
        (c.layout.self_dir / "seed.md").write_text("核心种子特征-SEED-ONCE", "utf-8")
        ev0 = c.observe(
            body="qq", channel="group:9", account="u1", display_name="阿青", text="早呀", now=T0
        )
        c.remember(ev0, "喜欢桂花乌龙-FACT-ONCE", now=T0)
        ctx = SkillContext("qq", "group:9", ev0.actor.person_id, private=False, owner=False, now=T0)
        r1 = c.respond(ev0, now=T0, mode="required", skill_ctx=ctx)

        ev1 = c.observe(
            body="qq",
            channel="group:9",
            account="u1",
            display_name="阿青",
            text="接着聊一句",
            now=T0 + timedelta(seconds=20),
        )
        r2 = c.respond(ev1, now=T0 + timedelta(seconds=20), mode="optional", skill_ctx=ctx)

        ev2 = c.observe(
            body="qq",
            channel="group:9",
            account="u1",
            display_name="阿青",
            text="群里旁白",
            now=T0 + timedelta(seconds=40),
        )
        r3 = c.respond(ev2, now=T0 + timedelta(seconds=40), mode="strict", skill_ctx=ctx)

        tids_mode = {
            r1.result.raw["thread_id"],
            r2.result.raw["thread_id"],
            r3.result.raw["thread_id"],
        }
        assert len(tids_mode) == 1
        turns = requests(folder, "turn/start")
        assert "核心种子特征-SEED-ONCE" in text_of(turns[0])
        assert "喜欢桂花乌龙-FACT-ONCE" in text_of(turns[0])
        for follow in turns[1:]:
            payload = text_of(follow)
            assert "核心种子特征-SEED-ONCE" not in payload
            assert "喜欢桂花乌龙-FACT-ONCE" not in payload
            assert "【换个说法】" not in payload
        assert "这条消息没有直接叫你" in text_of(turns[1])
        assert "这条消息不是冲你来的" in text_of(turns[2])
        assert len(text_of(turns[1])) < len(text_of(turns[0])) * 0.35


def test_group_speakers_with_different_familiarity_keep_single_thread(tmp_path, monkeypatch):
    from companion.skills.native import NativeSkills
    from companion.skills.reminder import Promises

    folder = tmp_path / "codex"
    b = backend(folder, monkeypatch)
    with companion(tmp_path, b) as c:
        c.skills.all.append(Promises(c.config.skills.reminders, c.reminders, c.people))
        p_close = c.people.ensure("qq", "1001", "老友")
        p_casual = c.people.ensure("qq", "1002", "新群友")
        c.people.set_familiarity(p_close, "熟络")
        c.people.set_familiarity(p_casual, "相熟")

        tids = []
        for idx, (acc, name, pid) in enumerate(
            [
                ("1001", "老友", p_close),
                ("1002", "新群友", p_casual),
                ("1001", "老友", p_close),
                ("1002", "新群友", p_casual),
            ]
        ):
            now = T0 + timedelta(seconds=idx * 15)
            ev = c.observe(
                body="qq",
                channel="group:77",
                account=acc,
                display_name=name,
                text=f"发言{idx}",
                now=now,
            )
            ctx = SkillContext("qq", "group:77", pid, private=False, owner=False, now=now)
            rep = c.respond(ev, now=now, mode="required", skill_ctx=ctx)
            tids.append(rep.result.raw["thread_id"])

        assert len(set(tids)) == 1
        assert len(requests(folder, "thread/start")) == 1
        turns = requests(folder, "turn/start")
        assert "你对老友的了解" in text_of(turns[0])
        assert "你对新群友的了解" in text_of(turns[1])
        assert "你对老友的了解" not in text_of(turns[2])
        assert "你对新群友的了解" not in text_of(turns[3])

        # 新群友（相熟）在工具执行层仍无权调用群约定工具
        ctx_casual = SkillContext("qq", "group:77", p_casual, private=False, owner=False, now=T0)
        ns = NativeSkills(c.skills, ctx_casual, lambda call, out: None)
        pact_tool = next(s["name"] for s in ns.tools.specs if "约定" in s["description"])
        res = ns.call(pact_tool, {"arg": "每天｜喝水", "content": ""}, "c1")
        assert res["success"] is False


def test_fact_correction_and_mood_revert_update_in_place_without_rotating_thread(
    tmp_path, monkeypatch
):
    from companion.core.extract import Extraction
    from companion.memory.index import Fact

    folder = tmp_path / "codex"
    b = backend(folder, monkeypatch)
    with companion(tmp_path, b) as c:
        ev0 = c.observe(body="qq", channel="private:1", account="one", text="我喜欢美式", now=T0)
        pid = ev0.actor.person_id
        old_fact = Fact(
            subject=pid,
            predicate="drink",
            statement="喜欢喝冰美式-OLD",
            valid_from=T0,
            event_ids=[ev0.id],
        )
        c.index.add_fact(old_fact, now=T0)
        c.life.note_mood("平静", "读书", channel="private:1", person_id=pid, now=T0)

        ctx0 = SkillContext("qq", "private:1", pid, private=True, owner=True, now=T0)
        r0 = c.respond(ev0, now=T0, skill_ctx=ctx0)
        tid = r0.result.raw["thread_id"]
        assert "喜欢喝冰美式-OLD" in text_of(requests(folder, "turn/start")[-1])

        # 心情变成“小得意”，随后又回到一模一样的“平静（读书）”：A -> B -> A 都应投递
        t1 = T0 + timedelta(minutes=1)
        c.life.note_mood("小得意", "被夸了", channel="private:1", person_id=pid, now=t1)
        say(c, "夸夸你", 60)
        assert "小得意" in text_of(requests(folder, "turn/start")[-1])

        t2 = T0 + timedelta(minutes=2)
        c.life.note_mood("平静", "读书", channel="private:1", person_id=pid, now=t2)
        # 同时模拟空闲记忆抽取产生了 supersedes 纠正事实：不应轮换线程，而是在下一轮增量刷新事实
        new_fact = Fact(
            subject=pid,
            predicate="drink",
            statement="改喝热桂花茶-NEW",
            valid_from=t2,
            event_ids=[ev0.id],
        )
        c._store(Extraction(episode=None, facts=[(new_fact, [old_fact.id])]))
        r2 = say(c, "现在喝什么", 120)
        assert r2.result.raw["thread_id"] == tid
        last_payload = text_of(requests(folder, "turn/start")[-1])
        assert "改喝热桂花茶-NEW" in last_payload
        assert "喜欢喝冰美式-OLD" not in last_payload
        assert "平静" in last_payload
