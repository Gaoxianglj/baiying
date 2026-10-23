"""测试用替身：不依赖网络和真实模型。"""

from __future__ import annotations

import zlib

import numpy as np

from companion.models.gateway import ModelGateway, PricedBackend
from companion.models.openai_compat import ChatMessage, ChatResult, ModelError


class ScriptedChat:
    """假的 OpenAI 兼容客户端：用 `handler(messages, json_mode)` 生成回复，并记下每次请求。"""

    def __init__(self, handler) -> None:
        self.model = "scripted"
        self.handler = handler
        self.requests: list[list[ChatMessage]] = []

    def chat(self, messages, *, json_mode=False, temperature=None, max_tokens=None) -> ChatResult:
        self.requests.append(messages)
        return ChatResult(
            text=self.handler(messages, json_mode), backend="fallback", model="scripted"
        )

    def close(self) -> None:
        pass


def scripted_gateway(handler) -> tuple[ModelGateway, ScriptedChat]:
    chat = ScriptedChat(handler)
    return ModelGateway(fallback=PricedBackend(client=chat, price_in=0, price_out=0)), chat


class FakeEmbedder:
    """把文本的二字组哈希进固定维度，再归一化：字面越像，余弦越高。"""

    def __init__(self, model: str = "fake-embed", dim: int = 128) -> None:
        self.model = model
        self.dim = dim
        self.calls = 0
        self.broken = False

    def embed(self, texts: list[str]) -> np.ndarray:
        if self.broken:
            raise ModelError("embedding 服务挂了", retryable=True)
        self.calls += 1
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            grams = [text[i : i + 2] for i in range(max(len(text) - 1, 1))]
            for g in grams:
                out[row, zlib.crc32(g.encode()) % self.dim] += 1
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)
