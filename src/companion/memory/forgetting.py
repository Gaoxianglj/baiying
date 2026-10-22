"""记忆强度：艾宾浩斯式遗忘曲线。

    R = exp(-Δt / S)          R 可回忆程度，Δt 距上次回忆的天数，S 稳定度（天）
    S_new = S · (1 + k · I)   每次被回忆，按重要度 I 变牢固

参数集中在这里，阶段 3（P3-04）调优。
"""

from __future__ import annotations

import math
from datetime import datetime

BASE_STABILITY_DAYS = 1.0
RECALL_GAIN = 1.0  # k
PINNED_RETRIEVABILITY = 1.0

# 状态阈值：R 低于它们时进入淡化 / 沉睡（P1-05 的睡眠任务使用）
FADED_BELOW = 0.3
DORMANT_BELOW = 0.05


def initial_stability(importance: float, emotion: float) -> float:
    """新记忆的稳定度：越重要、情绪越强烈，忘得越慢。importance、emotion 取 0..1。"""
    return BASE_STABILITY_DAYS * (1 + 4 * _clamp(importance)) * (1 + _clamp(emotion))


def retrievability(stability_days: float, last_recalled_at: datetime, now: datetime) -> float:
    elapsed_days = max((now - last_recalled_at).total_seconds() / 86400, 0.0)
    return math.exp(-elapsed_days / stability_days)


def stability_after_recall(stability_days: float, importance: float) -> float:
    return stability_days * (1 + RECALL_GAIN * _clamp(importance))


def _clamp(x: float) -> float:
    return min(max(x, 0.0), 1.0)
