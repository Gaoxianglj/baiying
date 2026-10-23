"""模型网关：按顺序尝试 GPT 订阅 → 兜底 API → 本地模型。

- 订阅：任一额度窗口用到阈值（或调用报额度错误）就暂停使用，直到窗口重置。
- 兜底 API：按量计费，记账；本月花费达到预算后不再使用。
- 本地模型：可选，最后一道兜底。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.openai_compat import (
    ChatMessage,
    ChatResult,
    ModelError,
    OpenAICompatClient,
)
from companion.models.usage import UsageLedger, estimate_cost

log = logging.getLogger(__name__)

Prefer = Literal["auto", "subscription", "fallback", "local"]

RATE_LIMIT_CACHE = timedelta(seconds=60)
DEFAULT_COOLDOWN = timedelta(minutes=10)


class ChatBackend(Protocol):
    name: str

    def chat(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult: ...


@dataclass
class PricedBackend:
    client: OpenAICompatClient
    price_in: float
    price_out: float
    monthly_budget_usd: float | None = None  # None 表示不限（如本地模型）


@dataclass
class BackendStatus:
    name: str
    available: bool
    detail: str


class ModelGateway:
    def __init__(
        self,
        *,
        subscription: CodexAppServerBackend | None = None,
        subscription_error: str | None = None,
        switch_at_used_percent: float = 99.0,
        fallback: PricedBackend | None = None,
        local: PricedBackend | None = None,
        ledger: UsageLedger | None = None,
    ) -> None:
        self.subscription = subscription
        self.subscription_error = subscription_error
        self.switch_at = switch_at_used_percent
        self.fallback = fallback
        self.local = local
        self.ledger = ledger
        self.prefer: Prefer = "auto"
        self._paused_until: datetime | None = None
        self._limits_checked_at: datetime | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        if self.subscription is not None:
            self.subscription.close()
        for b in (self.fallback, self.local):
            if b is not None:
                b.client.close()

    # ---- 调用 ----

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        purpose: str = "chat",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        now: datetime | None = None,
    ) -> ChatResult:
        errors: list[str] = []
        for name in self._order():
            try:
                if name == "subscription":
                    if not self._subscription_ready(now):
                        continue
                    assert self.subscription is not None
                    result = self.subscription.chat(
                        messages, json_schema=json_schema, max_tokens=max_tokens
                    )
                else:
                    priced = self.fallback if name == "fallback" else self.local
                    if priced is None or not self._within_budget(name, priced, now):
                        continue
                    result = priced.client.chat(
                        messages, json_mode=json_schema is not None, max_tokens=max_tokens
                    )
                    self._record(name, priced, result, purpose, now)
            except ModelError as e:
                errors.append(f"{name}：{e}")
                log.warning("%s 调用失败：%s", name, e)
                if name == "subscription" and e.retryable:
                    self._pause_subscription(now)
                if not e.retryable and self.prefer == name:
                    break
                continue
            if json_schema is not None:
                result.text = _extract_json_text(result.text)
            return result
        raise ModelError("所有模型后端都不可用：" + ("；".join(errors) or "没有可用的后端"))

    def chat_json(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        *,
        purpose: str = "extract",
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], ChatResult]:
        result = self.chat(messages, purpose=purpose, json_schema=schema, now=now)
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError as e:
            raise ModelError(f"{result.backend} 没有返回合法 JSON：{result.text[:200]}") from e
        if not isinstance(data, dict):
            raise ModelError(f"{result.backend} 返回的 JSON 不是对象")
        return data, result

    # ---- 状态 ----

    def status(self, now: datetime | None = None) -> list[BackendStatus]:
        now = now or datetime.now(UTC)
        out: list[BackendStatus] = []
        if self.subscription is None:
            out.append(BackendStatus("subscription", False, self.subscription_error or "未启用"))
        else:
            limits = self.subscription.last_rate_limits
            used = f"，额度已用 {limits.max_used_percent():.0f}%" if limits else ""
            if self._paused_until and now < self._paused_until:
                detail = f"暂停至 {self._paused_until.astimezone():%m-%d %H:%M}{used}"
                out.append(BackendStatus("subscription", False, detail))
            else:
                out.append(BackendStatus("subscription", True, "可用" + used))
        for name, priced in (("fallback", self.fallback), ("local", self.local)):
            if priced is None:
                out.append(BackendStatus(name, False, "未配置"))
                continue
            spent = self.ledger.month_cost(name, now) if self.ledger else 0.0
            budget = priced.monthly_budget_usd
            within = budget is None or spent < budget
            detail = f"{priced.client.model}，本月 ${spent:.4f}" + (
                f" / ${budget:.2f}" if budget is not None else ""
            )
            out.append(BackendStatus(name, within, detail))
        return out

    # ---- 内部 ----

    def _order(self) -> list[str]:
        default = ["subscription", "fallback", "local"]
        if self.prefer == "auto":
            return default
        return [self.prefer] + [n for n in default if n != self.prefer]

    def _subscription_ready(self, now: datetime | None) -> bool:
        if self.subscription is None:
            return False
        now = now or datetime.now(UTC)
        with self._lock:
            if self._paused_until and now < self._paused_until:
                return False
            if self._limits_checked_at is None or now - self._limits_checked_at > RATE_LIMIT_CACHE:
                self._limits_checked_at = now
                try:
                    limits = self.subscription.read_rate_limits()
                except ModelError as e:
                    log.warning("读取订阅额度失败：%s", e)
                    return True  # 读不到额度也先试着用，失败会触发暂停
                if limits.max_used_percent() >= self.switch_at:
                    self._paused_until = limits.reset_time(self.switch_at) or now + DEFAULT_COOLDOWN
                    log.info("订阅额度已用到阈值，暂停至 %s", self._paused_until)
                    return False
            return True

    def _pause_subscription(self, now: datetime | None) -> None:
        now = now or datetime.now(UTC)
        limits = self.subscription.last_rate_limits if self.subscription else None
        reset = limits.reset_time(self.switch_at) if limits else None
        with self._lock:
            self._paused_until = reset if reset and reset > now else now + DEFAULT_COOLDOWN

    def _within_budget(self, name: str, priced: PricedBackend, now: datetime | None) -> bool:
        if priced.monthly_budget_usd is None or self.ledger is None:
            return True
        spent = self.ledger.month_cost(name, now)
        if spent >= priced.monthly_budget_usd:
            log.warning("%s 本月已花 $%.2f，达到预算，停止使用", name, spent)
            return False
        if spent >= 0.8 * priced.monthly_budget_usd:
            log.warning("%s 本月已花 $%.2f，超过预算的 80%%", name, spent)
        return True

    def _record(
        self,
        name: str,
        priced: PricedBackend,
        result: ChatResult,
        purpose: str,
        now: datetime | None,
    ) -> None:
        if self.ledger is None:
            return
        self.ledger.record(
            backend=name,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=estimate_cost(
                result.input_tokens, result.output_tokens, priced.price_in, priced.price_out
            ),
            purpose=purpose,
            now=now,
        )


def _extract_json_text(text: str) -> str:
    """去掉模型偶尔包在 JSON 外面的 ``` 代码块。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
