"""OpenAI 兼容接口的客户端：DeepSeek、Ollama 等都走这里。"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import httpx
import numpy as np


class ModelError(RuntimeError):
    """模型调用失败。`retryable` 表示换一个后端重试有意义（限流、额度、网络）。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system / user / assistant / skill（技能返回的结果，发给 API 时按 user 处理）
    content: str
    images: tuple[Path, ...] = ()  # 本地图片（已转成 JPEG），只在 user 消息里使用
    event_id: str | None = None  # 稳定事件编号，仅用于本地持久会话去重，不发给 API

    def to_openai(self) -> dict[str, Any]:
        role = "user" if self.role == "skill" else self.role
        if not self.images:
            return {"role": role, "content": self.content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
        for path in self.images:
            b64 = base64.b64encode(path.read_bytes()).decode()
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            )
        return {"role": role, "content": parts}


@dataclass
class ChatResult:
    text: str
    backend: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str | None,
        api_key: str | None = None,
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.extra_body = extra_body or {}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        host = httpx.URL(base_url).host
        try:
            local = ip_address(host).is_loopback
        except ValueError:
            local = host.rstrip(".").lower() == "localhost"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport,
            trust_env=not local,  # 本地 Ollama 直连；远端 API 仍沿用用户代理与证书设置。
        )

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if not self.model:
            raise ModelError(f"{self.name} 没有配置模型名")
        body: dict[str, Any] = {
            **self.extra_body,
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        start = time.perf_counter()
        data = self._post("/chat/completions", body)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"{self.name} 返回格式异常：{str(data)[:200]}") from e
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            backend=self.name,
            model=data.get("model") or self.model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            seconds=time.perf_counter() - start,
            raw=data,
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        """返回 L2 归一化后的向量矩阵（行 = 文本）。"""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        data = self._post("/embeddings", {"model": self.model, "input": texts})
        try:
            rows = sorted(data["data"], key=lambda d: d["index"])
            matrix = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        except (KeyError, TypeError, ValueError) as e:
            raise ModelError(f"{self.name} 向量返回格式异常：{str(data)[:200]}") from e
        if matrix.ndim != 2 or len(matrix) != len(texts):
            raise ModelError(f"{self.name} 向量条数或维度异常：{matrix.shape}")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1, norms)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise ModelError(f"{self.name} 连接失败：{e}", retryable=True) from e
        if resp.status_code >= 400:
            # 401/403 是配置问题，换后端重试也没用；429 与 5xx 可以换后端
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise ModelError(
                f"{self.name} 返回 HTTP {resp.status_code}：{resp.text[:200]}", retryable=retryable
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise ModelError(
                f"{self.name} 返回了非 JSON 响应：{resp.text[:200]}", retryable=True
            ) from e
        if not isinstance(data, dict):
            raise ModelError(
                f"{self.name} 返回了异常 JSON 结构：{str(data)[:200]}", retryable=True
            )
        return data
