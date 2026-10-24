"""联网搜索与读网页：DeepSeek Search / Fetch API。

- 【搜索：关键词】→ POST /v1/tools/search，取前几条结果的标题、网址、摘要交回给她。
- 【打开：网址】→ POST /v1/tools/fetch，网页转成 Markdown 交回给她。
网页由 DeepSeek 的服务器去抓，不会从本机访问内网地址。
用兜底 API 的 key；每次成功调用按 0.75 美元 / 千次记进用量账本，计入 DeepSeek 月预算。
搜到的内容只用来回答，不直接进长期记忆（她说出口的回复照常记录）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from companion.config import FallbackConfig, SearchSkillConfig
from companion.models.usage import UsageLedger
from companion.skills.base import Skill, SkillContext, SkillError, SkillResult

log = logging.getLogger(__name__)

_ERRORS = {
    401: "DeepSeek API key 无效",
    402: "DeepSeek 余额不足",
    403: "这个网页不允许读取",
    404: "这个网页打不开，或者没有能读的内容",
    413: "内容太长了",
}


def _unescape(text: str) -> str:
    """去掉 Markdown 转义的反斜杠（如 \\- \\#），省字数也好读。"""
    return _ESCAPED.sub(r"\1", text)


_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~&/,:;=])")


class WebSearch(Skill):
    name = "search"
    label = "联网搜索"
    markers = {
        "搜索": "【搜索：关键词】在网上查最新的信息（新闻、天气、歌词、资料等），关键词要简短",
        "打开": "【打开：网址】读一个网页的内容（比如对方发来的链接，或搜索结果里的网址）",
    }

    def __init__(
        self,
        cfg: SearchSkillConfig,
        api: FallbackConfig,
        ledger: UsageLedger | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.api = api
        self.ledger = ledger
        self._transport = transport
        self._client: httpx.Client | None = None

    # ---- 可用性 ----

    def enabled(self) -> bool:
        return self.cfg.enabled and self.api.api_key is not None

    def allowed(self, ctx: SkillContext) -> bool:
        return ctx.private or self.cfg.in_groups

    def status(self) -> str:
        if self.api.api_key is None:
            return "缺少 DeepSeek API key"
        if self.ledger is None:
            return ""
        spent = self.ledger.month_cost("fallback")
        return f"DeepSeek 本月已花 ${spent:.2f} / ${self.api.monthly_budget_usd:g}"

    # ---- 执行 ----

    def run(self, marker: str, arg: str, ctx: SkillContext) -> SkillResult:
        self._check_budget()
        if marker == "打开":
            return self._fetch(arg)
        return self._search(arg)

    def _search(self, query: str) -> SkillResult:
        query = query.strip()[:128]
        if not query:
            raise SkillError("关键词是空的")
        data = self._post("search", {"query": query, "count": self.cfg.count})
        results = data.get("results") or []
        if not results:
            return SkillResult(f"搜索“{query}”没有找到结果。")
        self._charge("search")
        lines = [f"搜索“{query}”的结果："]
        for i, r in enumerate(results[: self.cfg.count], 1):
            meta = "，".join(x for x in (r.get("site_name"), (r.get("date") or "")[:10]) if x)
            head = f"{i}. {r.get('title') or '（无标题）'}" + (f"（{meta}）" if meta else "")
            body = " ".join(_unescape(r.get("summary") or r.get("snippet") or "").split())[:500]
            lines.append(f"{head}\n   {r.get('url', '')}\n   {body}")
        return SkillResult("\n".join(lines))

    def _fetch(self, url: str) -> SkillResult:
        url = url.strip().strip("<>")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or "@" in parsed.netloc:
            raise SkillError("这不是一个能打开的网址（需要 http 或 https 开头）")
        data = self._post("fetch", {"url": url})
        content = re.sub(r"\n{3,}", "\n\n", _unescape(data.get("content") or "")).strip()
        if not content:
            raise SkillError("这个网页没有能读的内容")
        self._charge("fetch")
        title = data.get("title") or url
        return SkillResult(f"网页：{title}\n{url}\n\n{content}")

    # ---- HTTP ----

    def _post(self, tool: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.api.api_key is not None
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.api.base_url.rstrip("/"),
                timeout=self.cfg.timeout_seconds,
                transport=self._transport,
            )
        headers = {"Authorization": f"Bearer {self.api.api_key.get_secret_value()}"}
        for attempt in range(2):
            try:
                resp = self._client.post(f"/v1/tools/{tool}", json=body, headers=headers)
            except httpx.HTTPError as e:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise SkillError("连不上 DeepSeek 的搜索服务") from e
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise SkillError("搜索服务暂时忙不过来，过会儿再试")
            log.warning("DeepSeek %s 返回 %s：%s", tool, resp.status_code, resp.text[:200])
            raise SkillError(_ERRORS.get(resp.status_code, f"请求失败（{resp.status_code}）"))
        raise SkillError("搜索服务暂时不可用")

    def _check_budget(self) -> None:
        if self.ledger is None:
            return
        if self.ledger.month_cost("fallback") >= self.api.monthly_budget_usd:
            raise SkillError("这个月的 DeepSeek 预算用完了，暂时不能联网")

    def _charge(self, tool: str) -> None:
        if self.ledger is not None:
            self.ledger.record(
                backend="fallback",
                model=f"tools/{tool}",
                input_tokens=0,
                output_tokens=0,
                cost_usd=self.cfg.price_per_call_usd,
                purpose="skill",
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
