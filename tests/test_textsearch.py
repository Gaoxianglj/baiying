from companion.memory.textsearch import index_text, match_query, tokenize


def test_cjk_bigrams_and_latin_words():
    assert tokenize("不加糖的Coffee 2杯") == ["不加", "加糖", "糖的", "coffee", "2", "杯"]


def test_index_includes_unigrams():
    assert set(index_text("我的猫").split()) == {"我的", "的猫", "我", "的", "猫"}


def test_query_dedupes_and_quotes():
    assert match_query("咖啡 咖啡") == '"咖啡"'
    assert match_query("……！") is None
