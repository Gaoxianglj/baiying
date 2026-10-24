"""OpenAI 兼容接口的客户端：DeepSeek、Ollama 等都走这里。"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
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
    role: str  # system / user / assistant
    content: str
    images: tuple[Path, ...] = ()  # 本地图片（已转成 JPEG），只在 user 消息里使用

    def to_openai(self) -> dict[str, Any]:
        if not self.images:
            return {"role": self.role, "content": self.content}
        parts: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
        for path in self.images:
            b64 = base64.b64encode(path.read_bytes()).decode()
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            )
        return {"role": self.role, "content": parts}


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
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
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
        rows = sorted(data["data"], key=lambda d: d["index"])
        matrix = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
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
        return resp.json()
