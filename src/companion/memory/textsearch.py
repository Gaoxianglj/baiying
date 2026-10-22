"""中文友好的全文检索分词。

SQLite FTS5 默认分词器把一整句中文当成一个词，trigram 分词器又搜不到两个字的词（如“咖啡”）。
这里在 Python 里把中日韩文字切成相邻二字组（“不加糖” → 不加 加糖），拉丁字母与数字按词小写，
再交给 FTS5 的 unicode61 分词器按空格切分。
"""

from __future__ import annotations

import re

_CJK = r"㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[0-9A-Za-zÀ-ɏ]+")
_CJK_RE = re.compile(rf"[{_CJK}]")


def tokenize(text: str, *, with_unigrams: bool = False) -> list[str]:
    """`with_unigrams` 用于建索引：额外收录单字，让一个字的查询（如“猫”）也能命中。"""
    tokens: list[str] = []
    for run in _TOKEN_RE.findall(text):
        if _CJK_RE.match(run):
            if len(run) == 1:
                tokens.append(run)
                continue
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
            if with_unigrams:
                tokens.extend(run)
        else:
            tokens.append(run.lower())
    return tokens


def index_text(text: str) -> str:
    """写入 FTS 表的文本。"""
    return " ".join(tokenize(text, with_unigrams=True))


def match_query(text: str) -> str | None:
    """把用户的查询变成 FTS5 MATCH 表达式：任一词命中即可，排序交给 bm25。"""
    tokens = list(dict.fromkeys(tokenize(text)))
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)
