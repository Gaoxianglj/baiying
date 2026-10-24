"""S-02 联网搜索与读网页（DeepSeek Search / Fetch API，用假服务器）。"""

import json

import httpx
import pytest
from pydantic import SecretStr

from companion.config import FallbackConfig, SearchSkillConfig
from companion.models.usage import UsageLedger
from companion.skills import SkillContext, SkillError
from companion.skills.search import WebSearch

CTX = SkillContext(body="qq", channel="group:1", person_id="p", private=False, owner=False)


def make(tmp_path, handler, **cfg):
    seen: list[httpx.Request] = []

    def record(request):
        seen.append(request)
        return handler(request)

    ledger = UsageLedger(tmp_path / "usage.jsonl")
    skill = WebSearch(
        SearchSkillConfig(**cfg),
        FallbackConfig(api_key=SecretStr("sk-test"), monthly_budget_usd=1.0),
        ledger,
        transport=httpx.MockTransport(record),
    )
    return skill, seen, ledger


RESULTS = {
    "results": [
        {"title": "今日天气", "url": "https://w.example/1", "summary": "晴，18 度",
         "site_name": "天气网", "date": "2026-10-24T08:00:00Z"},
        {"title": "明日天气", "url": "https://w.example/2", "snippet": "多云"},
    ]
}  # fmt: skip


def test_search_formats_results_and_charges(tmp_path):
    skill, seen, ledger = make(tmp_path, lambda r: httpx.Response(200, json=RESULTS), count=3)
    result = skill.run("搜索", " 北京天气 ", CTX)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/tools/search"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert body == {"query": "北京天气", "count": 3}
    assert "1. 今日天气（天气网，2026-10-24）" in result.text
    assert "https://w.example/1" in result.text and "晴，18 度" in result.text
    assert "多云" in result.text
    assert ledger.month_cost("fallback") == pytest.approx(0.00075)


def test_empty_search_is_not_charged(tmp_path):
    skill, _, ledger = make(tmp_path, lambda r: httpx.Response(200, json={"results": []}))
    assert "没有找到" in skill.run("搜索", "xyz", CTX).text
    assert ledger.month_cost("fallback") == 0


def test_fetch_page(tmp_path):
    page = {
        "title": "猫的百科",
        "content": "# 猫\n\n\n\n猫是一种小型哺乳动物\\, in\\-house \\/ 家猫。",
    }
    skill, seen, ledger = make(tmp_path, lambda r: httpx.Response(200, json=page))
    result = skill.run("打开", "<https://cat.example/wiki>", CTX)
    assert json.loads(seen[0].content) == {"url": "https://cat.example/wiki"}
    assert result.text.startswith("网页：猫的百科")
    assert "# 猫\n\n猫是一种小型哺乳动物, in-house / 家猫。" in result.text
    assert ledger.month_cost("fallback") == pytest.approx(0.00075)


@pytest.mark.parametrize("url", ["file:///C:/secret", "javascript:alert(1)", "https://u:p@x.com"])
def test_fetch_rejects_bad_urls(tmp_path, url):
    skill, seen, _ = make(tmp_path, lambda r: httpx.Response(200, json={}))
    with pytest.raises(SkillError):
        skill.run("打开", url, CTX)
    assert seen == []


def test_errors_are_readable(tmp_path):
    def handler(r):
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    skill, _, _ = make(tmp_path, handler)
    with pytest.raises(SkillError, match="余额不足"):
        skill.run("搜索", "x", CTX)


def test_retries_once_on_429(tmp_path, monkeypatch):
    monkeypatch.setattr("companion.skills.search.time.sleep", lambda s: None)
    replies = [httpx.Response(429), httpx.Response(200, json=RESULTS)]
    skill, seen, _ = make(tmp_path, lambda r: replies.pop(0))
    assert "今日天气" in skill.run("搜索", "x", CTX).text
    assert len(seen) == 2


def test_budget_and_availability(tmp_path):
    skill, seen, ledger = make(tmp_path, lambda r: httpx.Response(200, json=RESULTS))
    ledger.record(backend="fallback", model="m", input_tokens=0, output_tokens=0,
                  cost_usd=1.0, purpose="chat")  # fmt: skip
    with pytest.raises(SkillError, match="预算"):
        skill.run("搜索", "x", CTX)
    assert seen == []
    assert "1.00 / $1" in skill.status()
    # 没有 key 就不可用；可以只在私聊里用
    assert not WebSearch(SearchSkillConfig(), FallbackConfig()).enabled()
    private_only, _, _ = make(tmp_path, lambda r: None, in_groups=False)
    assert not private_only.allowed(CTX)
