import json
import tomllib

import pytest

from companion.config import load_config
from companion.core.companion import Companion
from companion.memory import Fact, MemoryLayout, init_memory
from companion.web import settings
from companion.web.server import COOKIE, ConsoleApp
from tests.fakes import FakeEmbedder, scripted_gateway

TOKEN = "t0ken-for-tests"
HOST = {"host": "127.0.0.1:8765"}
AUTH = {**HOST, "cookie": f"{COOKIE}={TOKEN}"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    for var in ("QQ_GROUPS", "FALLBACK_MODEL", "QQ_OWNER_ACCOUNTS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.toml").write_text('[persona]\nname = "白璎"\n', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "# 注释保留\nFALLBACK_API_KEY=sk-secret\nQQ_GROUPS=111\n", encoding="utf-8"
    )
    init_memory(MemoryLayout(tmp_path / "data" / "memory"), "白璎", git=False)
    return tmp_path


@pytest.fixture
def app(root):
    config = load_config(root)
    gateway, _ = scripted_gateway(lambda m, j: "好")
    c = Companion(config, gateway=gateway, embedder=FakeEmbedder())
    a = ConsoleApp(root, TOKEN, companion=c)
    yield a
    a.close()


def call(app, method, path, body=None, headers=AUTH):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = {**headers, "content-type": "application/json"} if body is not None else headers
    status, extra, payload = app.handle(method, path, hdrs, raw)
    data = (
        json.loads(payload)
        if extra.get("Content-Type", "").startswith("application/json")
        else payload
    )
    return status, extra, data


def test_requires_token_cookie_and_local_host(app):
    assert call(app, "GET", "/api/overview", headers=HOST)[0] == 401
    assert call(app, "GET", "/api/overview", headers={**AUTH, "host": "evil.com"})[0] == 403
    status, extra, _ = call(app, "GET", f"/?token={TOKEN}", headers=HOST)
    assert status == 303 and TOKEN in extra["Set-Cookie"] and "HttpOnly" in extra["Set-Cookie"]
    assert call(app, "GET", "/?token=wrong", headers=HOST)[0] == 401
    status, extra, page = call(app, "GET", "/")
    assert status == 200 and "白璎控制台" in page.decode()


def test_trpg_console_requires_auth_explicit_kp_and_deduplicates_request(app):
    assert call(app, "GET", "/api/trpg", headers=HOST)[0] == 401
    assert call(app, "POST", "/api/trpg", {"operation": "创建", "channel": "group:111",
                                          "args": {"name": "控制台团"}})[0] == 200
    rows = call(app, "GET", "/api/trpg")[2]
    cid = rows["campaigns"][0]["id"]
    assert "today" in rows
    assert call(app, "POST", "/api/trpg", {"campaign": cid, "operation": "暂停"})[0] == 400
    payload = {"campaign": cid, "as_kp": True, "operation": "推进时间",
               "request_id": "same", "args": {"minutes": 3}}
    first = call(app, "POST", "/api/trpg", payload)[2]
    again = call(app, "POST", "/api/trpg", payload)[2]
    assert first["changed"] and again["replay"]
    public = call(app, "POST", "/api/trpg", {"campaign": cid, "operation": "状态"})[2]["data"]
    assert public["minutes"] == 3 and "state" not in public


def test_settings_describe_sources(root):
    fields = {f["path"]: f for f in settings.describe(root)}
    assert fields["qq.groups"]["source"] == ".env" and fields["qq.groups"]["value"] == ["111"]
    assert fields["persona.name"]["source"] == "config.toml"
    assert fields["sleep.hour"]["source"] == "默认"
    assert not any("api_key" in p or "access_token" in p for p in fields)


def test_save_goes_to_local_toml_and_env(root):
    written = settings.save(
        root, {"sleep.hour": 5, "qq.groups": "111, 222", "qq.unsolicited_probability": 0.5}
    )
    assert set(written) == {"config.local.toml", ".env"}
    local = tomllib.loads((root / "config.local.toml").read_text("utf-8"))
    assert local["sleep"]["hour"] == 5 and local["qq"]["unsolicited_probability"] == 0.5
    env = (root / ".env").read_text("utf-8")
    assert (
        "QQ_GROUPS=111,222" in env and "FALLBACK_API_KEY=sk-secret" in env and "# 注释保留" in env
    )
    config = load_config(root)
    assert config.qq.groups == ["111", "222"] and config.sleep.hour == 5


def test_invalid_values_write_nothing(root):
    with pytest.raises(settings.SettingsError):
        settings.save(root, {"sleep.hour": 5, "sleep.identity_every_days": 999})
    with pytest.raises(settings.SettingsError):
        settings.save(root, {"models.fallback.api_key": "x"})
    with pytest.raises(settings.SettingsError):
        settings.save(root, {"models.subscription.effort": "turbo"})
    assert not (root / "config.local.toml").exists()


def test_empty_text_resets_to_default(root):
    settings.save(root, {"models.subscription.model": "gpt-6-luna"})
    assert load_config(root).models.subscription.model == "gpt-6-luna"
    settings.save(root, {"models.subscription.model": ""})
    assert load_config(root).models.subscription.model is None
    settings.save(root, {"sleep.hour": 6})
    settings.reset(root, "sleep.hour")
    assert load_config(root).sleep.hour == 4


def test_dump_toml_round_trip():
    data = {
        "a": 1,
        "persona": {"name": '白璎"引号'},
        "qq": {"aliases": ["白璎", "小璎"]},
        "models": {"subscription": {"enabled": False, "switch_at_used_percent": 99.5}},
    }
    assert tomllib.loads(settings.dump_toml(data)) == data


def test_api_overview_people_seed_forget_journal(app, root):
    c = app.companion
    pid = c.people.ensure("qq", "10001", "小明")
    event = c.observe(body="qq", channel="private:10001", account="10001", text="我对花生过敏")
    fact = c.remember(event, "我对花生过敏")

    status, _, ov = call(app, "GET", "/api/overview")
    assert status == 200 and ov["memory"]["facts"] == 1 and ov["fallback"]["key_set"] is True
    assert "sk-secret" not in json.dumps(ov)

    _, _, people = call(app, "GET", "/api/people")
    assert people["people"][0]["id"] == pid and people["people"][0]["facts"] == 1
    _, _, person = call(app, "GET", f"/api/people/{pid}")
    assert person["facts"][0]["pinned"] is True

    status, _, _ = call(app, "POST", "/api/seed", {"text": "# 种子\n\n- 我很温柔\n"})
    assert status == 200
    _, _, self_files = call(app, "GET", "/api/self")
    assert "我很温柔" in self_files["seed"]

    status, _, r = call(app, "POST", "/api/forget", {"kind": "fact", "id": fact.id})
    assert status == 200 and r["memories"] == 1 and r["messages"] == 1
    assert c.index.get_fact(fact.id) is None

    (c.layout.journal_dir / "2026-10-23.md").write_text("# 2026-10-23\n\n今天很开心。\n", "utf-8")
    _, _, journal = call(app, "GET", "/api/journal")
    assert journal["entries"][0]["date"] == "2026-10-23"
    _, _, entry = call(app, "GET", "/api/journal/2026-10-23")
    assert entry["text"] == "今天很开心。"
    assert call(app, "GET", "/api/journal/..%2Fseed")[0] == 400


def test_api_today(app):
    from datetime import UTC, datetime

    from companion.life import DayPlan
    from companion.life.days import SLOT_NAMES

    life = app.companion.life
    now = datetime.now(UTC)
    life.days.set_plan(now, DayPlan("安静", "《雪国》", {n: f"{n}在读书" for n in SLOT_NAMES}))
    life.note_mood("开心", "被夸了", channel="private:1", person_id=None, now=now)
    act = {"ts": now.isoformat(), "kind": "write", "title": "写了《初雪》", "summary": "雪"}
    life.days.append(now, "activities", act)
    life.owner.record(now, "morning", "早呀")
    life.owner.record(now, "share", "", skipped=True)
    folder = app.companion.layout.self_dir / "writings"
    folder.mkdir(parents=True)
    (folder / "2026-10-27-1500.md").write_text("# 初雪\n\n雪落在书页上。\n", "utf-8")

    status, _, d = call(app, "GET", "/api/today")
    assert status == 200 and d["plan"]["reading"] == "《雪国》"
    assert d["mood"]["word"] == "开心" and d["slot"] in SLOT_NAMES
    assert [p["kind"] for p in d["proactive"]] == ["morning"]  # 跳过的不显示
    assert d["writings"][0] == {
        "name": "2026-10-27-1500",
        "title": "初雪",
        "text": "雪落在书页上。",
    }


def test_life_settings_validate(root):
    settings.save(root, {"life.owner_chat.morning": "7:45", "life.activities.budget_percent": 10})
    config = load_config(root)
    assert config.life.owner_chat.morning == "07:45"
    assert config.life.activities.budget_percent == 10
    with pytest.raises(settings.SettingsError):
        settings.save(root, {"life.owner_chat.night": "半夜"})


def test_post_requires_json(app):
    status, _, _ = app.handle("POST", "/api/seed", {**AUTH, "content-type": "text/plain"}, b"x")
    assert status == 415


def test_fact_fixture_unused_guard():
    # Fact 在这里导入是为了确保公开接口没有被改名
    assert Fact.model_fields["pinned"]
