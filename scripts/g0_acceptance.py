"""G0 验收：重启或换模型后，仍能回忆同一组事实。

流程：在临时记忆里告诉白璎 5 条个人事实 → 整理成记忆 → 重新装配（模拟重启）→
在另一个频道提问（答案只能来自长期记忆，不在最近对话里）→ 换一个模型后端再问一遍。

用法：uv run python scripts/g0_acceptance.py [--backends subscription fallback]
默认先用订阅（不可用时自动跳过），再用兜底 API。只会花很少的 API 费用。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion.config import load_config
from companion.core.companion import Companion
from companion.memory import MemoryLayout, init_memory
from companion.models.openai_compat import ModelError

ROOT = Path(__file__).resolve().parents[1]

TELL = [
    "我最喜欢的颜色是靛蓝色，衣服基本都是这个色系。",
    "我家养了一只柯基，名字叫豆豆，特别贪吃。",
    "对了提醒一下，我对花生过敏，吃到会很严重。",
    "我喝咖啡从来不加糖，但是一定要加奶。",
    "下个月我要去京都旅行，已经订好民宿了。",
]

# (问题, 关键词组)：每组里任意一个词出现即可，所有组都要满足
ASK = [
    ("你还记得我最喜欢什么颜色吗？", [["靛蓝", "靛青"]]),
    ("我家那只狗叫什么来着？", [["豆豆"]]),
    ("我要是请你吃零食，有什么要避开的吗？", [["花生"]]),
    ("帮我点杯咖啡的话，你会怎么点？", [["不加糖", "无糖", "不要糖", "别加糖", "不放糖"], ["奶"]]),
    ("我下个月要去哪儿玩来着？", [["京都"]]),
]


def check(answer: str, groups: list[list[str]]) -> bool:
    return all(any(word in answer for word in group) for group in groups)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", nargs="+", default=["subscription", "fallback"])
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="g0-"))
    os.environ["COMPANION_DATA_DIR"] = str(tmp)
    config = load_config(ROOT)
    layout = MemoryLayout(config.memory_dir)
    init_memory(layout, config.persona.name, git=False)
    seed = ROOT / "data" / "memory" / "self" / "seed.md"
    if seed.exists():
        shutil.copy(seed, layout.self_dir / "seed.md")

    t0 = datetime.now(UTC) - timedelta(days=2)  # 两天前告诉她，今天来问
    print(f"临时记忆：{tmp}\n\n== 第一步：告诉她 5 条事实 ==")
    with Companion(config) as c:
        c.gateway.prefer = "fallback"  # 告知阶段用哪个后端不重要
        for i, text in enumerate(TELL):
            r = c.reply(
                body="cli", channel="cli:g0-tell", account="tester", display_name="测试者",
                text=text, now=t0 + timedelta(minutes=i),
            )  # fmt: skip
            print(f"我：{text}\n她：{r.text}")
        done = c.extract_pending(force=True, now=t0 + timedelta(hours=1))
        facts = [f.statement for x in done for f, _ in x.facts]
        print(f"\n整理出 {len(facts)} 条事实：")
        for s in facts:
            print(f"  - {s}")
        statuses = {s.name: s for s in c.gateway.status()}

    results: list[tuple[str, str, str, bool, str]] = []
    for backend in args.backends:
        status = statuses.get(backend)
        if status is None or not status.available:
            print(f"\n== 跳过 {backend}：{status.detail if status else '未知后端'} ==")
            continue
        print(f"\n== 第二步：重新装配后，用 {backend} 在新频道提问 ==")
        with Companion(config) as c:  # 每次都从磁盘重新装配，相当于重启
            c.gateway.prefer = backend  # type: ignore[assignment]
            for i, (question, groups) in enumerate(ASK):
                try:
                    r = c.reply(
                        body="cli", channel=f"cli:g0-ask-{backend}", account="tester",
                        display_name="测试者", text=question,
                        now=datetime.now(UTC) + timedelta(minutes=i),
                    )  # fmt: skip
                except ModelError as e:
                    results.append((backend, question, f"（出错：{e}）", False, "-"))
                    continue
                ok = check(r.text, groups) and r.result.backend == backend
                results.append((backend, question, r.text, ok, r.result.backend))
                print(f"{'通过' if ok else '未通过'}  我：{question}\n      她：{r.text}")

    print("\n== 结果 ==")
    by_backend: dict[str, list[bool]] = {}
    for backend, _, _, ok, actual in results:
        by_backend.setdefault(backend, []).append(ok)
        if actual not in (backend, "-"):
            print(f"注意：请求 {backend}，实际由 {actual} 回答")
    for backend, oks in by_backend.items():
        print(f"{backend}：{sum(oks)}/{len(oks)}")
    tested = list(by_backend)
    passed = len(tested) > 0 and all(all(v) for v in by_backend.values())
    if len(tested) < 2:
        print("只测了一个后端：G0 的“换模型”部分需要两个可用后端才算完整通过。")
    print("G0", "通过" if passed and len(tested) >= 2 else ("部分通过" if passed else "未通过"))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
