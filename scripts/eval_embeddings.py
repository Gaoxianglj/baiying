"""比较本地 embedding 模型在白璎这类中文记忆上的检索效果。

用法：uv run python scripts/eval_embeddings.py
在临时目录里建一套测试记忆，不碰真实记忆文件夹。
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion.config import EmbeddingConfig, RetrievalConfig
from companion.core.retrieval import HybridRetriever, sync_vectors
from companion.memory import Episode, Fact, MemoryIndex, MemoryLayout, init_memory
from companion.models.openai_compat import OpenAICompatClient

T0 = datetime(2026, 7, 1, tzinfo=UTC)

# (id, 文本)：e 开头是情景，f 开头是事实
MEMORIES = [
    ("e_house", "和小明在 MC 里盖了一座橡木小屋，他两次掉进岩浆，笑得停不下来"),
    ("e_nether", "大家约好周末一起去下界找要塞，结果迷路了两个小时"),
    ("e_poem", "小红分享了一首海子的诗，聊到深夜，她说最近工作压力很大"),
    ("e_exam", "阿杰说下个月要考研，最近每天图书馆待到闭馆"),
    ("e_cat", "小明家的猫生了三只小猫，他发了好多照片到群里"),
    ("e_movie", "群里一起看了《星际穿越》，讨论了很久时间膨胀的问题"),
    ("e_birthday", "今天是小红的生日，大家在群里给她唱了生日歌"),
    ("e_fight", "阿杰和小明因为红石电路的设计吵了一架，后来又和好了"),
    ("e_rain", "下了一整天雨，小红说她很喜欢雨声，适合读书"),
    ("e_farm", "白璎第一次在 MC 里自己造了一个自动甘蔗农场"),
    ("e_trip", "小明说十一假期去了杭州，在西湖边骑了自行车"),
    ("e_game", "大家讨论要不要一起开一个新的生存存档，最后决定用 1.21 版本"),
    ("f_coffee", "小明喜欢不加糖、加奶的咖啡"),
    ("f_allergy", "小红对花生过敏"),
    ("f_job", "阿杰是一名程序员，在一家游戏公司工作"),
    ("f_color", "小红最喜欢的颜色是靛蓝色"),
    ("f_music", "小明喜欢听后摇，最喜欢的乐队是 Mogwai"),
    ("f_city", "阿杰住在成都，喜欢吃火锅"),
    ("f_book", "小红最喜欢的书是《百年孤独》"),
    ("f_pet", "阿杰养了一只叫豆豆的柯基"),
    ("f_sleep", "小明经常熬夜，一般凌晨两点才睡"),
    ("f_lang", "小红在学日语，打算明年去京都旅行"),
]

# (查询, 期望命中的 id)：多数刻意换了说法，不和原文共享关键词
QUERIES = [
    ("小明平时喝咖啡有什么讲究", "f_coffee"),
    ("给小明点一杯饮料该怎么点", "f_coffee"),
    ("小红有什么不能吃的东西", "f_allergy"),
    ("阿杰是做什么工作的", "f_job"),
    ("小红偏爱哪种色彩", "f_color"),
    ("小明爱听什么类型的音乐", "f_music"),
    ("阿杰在哪座城市生活", "f_city"),
    ("小红最爱读的小说", "f_book"),
    ("阿杰家的狗叫什么", "f_pet"),
    ("小明作息怎么样", "f_sleep"),
    ("小红在准备出国玩吗", "f_lang"),
    ("我们之前一起建的房子", "e_house"),
    ("上次去地狱探险的事", "e_nether"),
    ("小红心情不好的那天晚上", "e_poem"),
    ("阿杰在备考什么", "e_exam"),
    ("小明家添了新成员", "e_cat"),
    ("一起看的那部太空电影", "e_movie"),
    ("那次两个人因为机器设计闹矛盾", "e_fight"),
    ("白璎自己做的第一个自动化装置", "e_farm"),
    ("小明国庆出去玩了哪里", "e_trip"),
    # 精确的生僻词：关键词检索的用武之地
    ("Mogwai", "f_music"),
    ("豆豆最近怎么样", "f_pet"),
    ("百年孤独", "f_book"),
    ("西湖", "e_trip"),
    ("甘蔗", "e_farm"),
]

# 与所有记忆都无关的闲聊：用来看“不相关”的余弦大概多高
NOISE = ["今天天气怎么样", "晚饭吃什么好呢", "你会唱歌吗", "这道数学题怎么做", "早上好呀"]

QWEN_INSTRUCTION = "Instruct: 根据问题，检索相关的聊天记忆\nQuery: "

CANDIDATES = [
    ("qwen3-embedding:4b", ""),
    ("qwen3-embedding:4b", QWEN_INSTRUCTION),
    ("bge-m3", ""),
]


def build(index: MemoryIndex) -> None:
    for i, (mid, text) in enumerate(MEMORIES):
        start = T0 + timedelta(days=i)
        if mid.startswith("e_"):
            index.add_episode(
                Episode(
                    id=mid,
                    started_at=start,
                    ended_at=start + timedelta(hours=1),
                    body="qq",
                    channel="group:1",
                    summary=text,
                )
            )
        else:
            index.add_fact(
                Fact(id=mid, subject="x", predicate=mid, statement=text, valid_from=start)
            )


def evaluate(index: MemoryIndex, model: str, instruction: str) -> dict[str, float]:
    client = OpenAICompatClient(name="ollama", base_url="http://localhost:11434/v1", model=model)
    t = time.perf_counter()
    sync_vectors(index, client)
    embed_s = time.perf_counter() - t

    results: dict[str, float] = {"embed_s": embed_s}
    for mode in ("vector", "hybrid"):
        retriever = HybridRetriever(
            index,
            client,
            query_instruction=instruction,
            text_weight=RetrievalConfig().text_weight,
            min_cosine=EmbeddingConfig().min_cosine,
        )
        if mode == "vector":
            index_search_text = retriever.index.search_text
            retriever.index.search_text = lambda *a, **k: []  # type: ignore[method-assign]
        ranks = []
        t = time.perf_counter()
        for query, expected in QUERIES:
            ids = [c.memory_id for c in retriever.search(query, limit=10)]
            ranks.append(ids.index(expected) + 1 if expected in ids else None)
        per_query_ms = (time.perf_counter() - t) / len(QUERIES) * 1000
        if mode == "vector":
            retriever.index.search_text = index_search_text  # type: ignore[method-assign]
        results[f"{mode}_hit1"] = sum(r == 1 for r in ranks) / len(ranks)
        results[f"{mode}_hit3"] = sum(r is not None and r <= 3 for r in ranks) / len(ranks)
        results[f"{mode}_mrr"] = statistics.mean(1 / r if r else 0 for r in ranks)
        results[f"{mode}_ms"] = per_query_ms

    # 相关 vs 无关的余弦分布，用于定 min_cosine
    keys, matrix = index.load_vectors(model)
    pos = {k[1]: i for i, k in enumerate(keys)}
    rel = [float(matrix[pos[exp]] @ client.embed([instruction + q])[0]) for q, exp in QUERIES]
    noise = [float((matrix @ client.embed([instruction + q])[0]).max()) for q in NOISE]
    results["relevant_cos_min"] = min(rel)
    results["relevant_cos_median"] = statistics.median(rel)
    results["noise_cos_max"] = max(noise)
    client.close()
    return results


def main() -> int:
    rows = []
    for model, instruction in CANDIDATES:
        with tempfile.TemporaryDirectory() as tmp:
            layout = MemoryLayout(Path(tmp) / "memory")
            init_memory(layout, "白璎", git=False)
            with MemoryIndex(layout) as index:
                build(index)
                rows.append((model, bool(instruction), evaluate(index, model, instruction)))

    cols = [
        "vector_hit1", "vector_hit3", "vector_mrr", "hybrid_hit1", "hybrid_hit3", "hybrid_mrr",
        "hybrid_ms", "embed_s", "relevant_cos_min", "relevant_cos_median", "noise_cos_max",
    ]  # fmt: skip
    print("model | 指令前缀 | " + " | ".join(cols))
    for model, instr, r in rows:
        print(f"{model} | {'是' if instr else '否'} | " + " | ".join(f"{r[c]:.3f}" for c in cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
