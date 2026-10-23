"""测试用替身：不依赖网络和真实模型。"""

from __future__ import annotations

import zlib

import numpy as np

from companion.models.openai_compat import ModelError


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
