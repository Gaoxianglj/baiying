import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from companion.models.codex_app_server import (
    CodexAppServerBackend,
    parse_rate_limits,
    talk_only_overrides,
)
from companion.models.gateway import ModelGateway, PricedBackend
from companion.models.openai_compat import ChatMessage, ModelError, OpenAICompatClient
from companion.models.usage import UsageLedger

FAKE = str(Path(__file__).with_name("fake_codex_app_server.py"))
MESSAGES = [ChatMessage("system", "你是白璎"), ChatMessage("user", "在吗")]
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def codex(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return CodexAppServerBackend(command=[sys.executable, FAKE], workdir=tmp_path, timeout=10)


def deepseek(reply="（兜底）你好", status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000},
            },
        )

    client = OpenAICompatClient(
        name="fallback",
        base_url="https://api.example.com",
        model="deepseek-flash",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )
    return PricedBackend(client=client, price_in=0.3, price_out=1.2, monthly_budget_usd=20)


def test_codex_chat_round_trip(tmp_path, monkeypatch):
    backend = codex(tmp_path, monkeypatch, FAKE_REPLY="你好呀，我在")
    try:
        result = backend.chat(MESSAGES)
        assert result.text == "你好呀，我在" and result.backend == "subscription"
        assert backend.chat(MESSAGES).text == "你好呀，我在"  # 同一进程可连续多轮
        limits = backend.read_rate_limits()
        assert limits.max_used_percent() == 10
    finally:
        backend.close()


def test_codex_usage_limit_is_retryable(tmp_path, monkeypatch):
    backend = codex(tmp_path, monkeypatch, FAKE_FAIL="usage_limit")
    try:
        with pytest.raises(ModelError) as exc:
            backend.chat(MESSAGES)
        assert exc.value.retryable
    finally:
        backend.close()


def test_missing_codex_binary():
    with pytest.raises(ModelError, match="Codex CLI"):
        CodexAppServerBackend.from_codex("definitely-not-codex-xyz", Path("."))


def test_talk_only_processes_cannot_touch_the_computer(tmp_path, monkeypatch):
    """聊天进程不能自己看本机图片（2026-11-08 实测：不关时一句话就能让它看任意路径的图）。"""
    monkeypatch.setattr("companion.models.codex_app_server.shutil.which", lambda c: "codex.exe")
    chat = CodexAppServerBackend.from_codex("codex", tmp_path)
    for flag in ("features.view_image=false", "features.shell_tool=false",
                 "features.unified_exec=false", "features.image_generation=false",
                 'web_search="disabled"'):  # fmt: skip
        assert flag in chat._command, flag
    painter = talk_only_overrides(image=True)
    assert painter["features.image_generation"] == "true"
    assert painter["features.view_image"] == "false" and painter["web_search"] == '"disabled"'
    life = talk_only_overrides(image=True, web=True)
    assert life["web_search"] == '"live"' and life["features.view_image"] == "false"


def test_parse_rate_limits_multi_bucket():
    limits = parse_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1},
                    "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 2},
                }
            }
        }
    )
    assert limits.max_used_percent() == 100
    assert limits.reset_time(99) == datetime.fromtimestamp(2, UTC)


def test_gateway_prefers_subscription(tmp_path, monkeypatch):
    calls = []
    gw = ModelGateway(
        subscription=codex(tmp_path, monkeypatch),
        fallback=deepseek(calls=calls),
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
    )
    try:
        assert gw.chat(MESSAGES).backend == "subscription"
        assert calls == []
    finally:
        gw.close()


def test_gateway_switches_when_quota_exhausted(tmp_path, monkeypatch):
    gw = ModelGateway(
        subscription=codex(tmp_path, monkeypatch, FAKE_USED_PERCENT=100),
        fallback=deepseek(),
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
    )
    try:
        result = gw.chat(MESSAGES, now=NOW)
        assert result.backend == "fallback"
        sub = next(s for s in gw.status(now=NOW) if s.name == "subscription")
        assert not sub.available and "暂停" in sub.detail
    finally:
        gw.close()


def test_gateway_switches_on_usage_limit_error(tmp_path, monkeypatch):
    gw = ModelGateway(
        subscription=codex(tmp_path, monkeypatch, FAKE_FAIL="usage_limit"),
        fallback=deepseek(),
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
    )
    try:
        assert gw.chat(MESSAGES, now=NOW).backend == "fallback"
        # 之后在冷却期内直接走兜底，不再尝试订阅
        assert gw.chat(MESSAGES, now=NOW).backend == "fallback"
    finally:
        gw.close()


def test_budget_and_cost_tracking(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    gw = ModelGateway(fallback=deepseek(), ledger=ledger)
    gw.chat(MESSAGES, now=NOW)
    # 1M 输入 × $0.3 + 0.1M 输出 × $1.2 = $0.42
    assert ledger.month_cost("fallback", NOW) == pytest.approx(0.42)
    ledger.record(
        backend="fallback", model="m", input_tokens=0, output_tokens=0, cost_usd=20,
        purpose="x", now=NOW,
    )  # fmt: skip
    with pytest.raises(ModelError, match="不可用"):
        gw.chat(MESSAGES, now=NOW)


def test_prefer_fallback_for_switch_test(tmp_path, monkeypatch):
    gw = ModelGateway(
        subscription=codex(tmp_path, monkeypatch),
        fallback=deepseek(),
        ledger=UsageLedger(tmp_path / "usage.jsonl"),
    )
    try:
        gw.prefer = "fallback"
        assert gw.chat(MESSAGES).backend == "fallback"
    finally:
        gw.close()


def test_json_mode_both_backends(tmp_path, monkeypatch):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    calls = []
    gw = ModelGateway(subscription=codex(tmp_path, monkeypatch))
    try:
        data, _ = gw.chat_json(MESSAGES, schema)
        assert data == {"ok": True}
    finally:
        gw.close()
    gw2 = ModelGateway(fallback=deepseek(reply='```json\n{"ok": false}\n```', calls=calls))
    data, _ = gw2.chat_json(MESSAGES, schema)
    assert data == {"ok": False}
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_auth_error_not_retried_on_other_backends(tmp_path):
    gw = ModelGateway(fallback=deepseek(status=401))
    with pytest.raises(ModelError, match="401"):
        gw.chat(MESSAGES)
