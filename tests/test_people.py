import pytest

from companion.memory import MemoryLayout, PeopleError, PeopleRegistry, init_memory


@pytest.fixture
def layout(tmp_path):
    layout = MemoryLayout(tmp_path / "memory")
    init_memory(layout, "白璎", git=False)
    return layout


def test_unknown_account_gets_its_own_person(layout):
    registry = PeopleRegistry(layout)
    pid = registry.ensure("qq", "10001", "小明")

    assert pid.startswith("p")
    assert registry.resolve("qq", "10001") == pid
    assert registry.ensure("qq", "10001", "小明") == pid  # 第二次不再建档
    text = layout.person_path(pid).read_text("utf-8")
    assert "# 小明" in text and "'10001'" in text  # 账号按字符串存


def test_new_nickname_is_remembered(layout):
    registry = PeopleRegistry(layout)
    pid = registry.ensure("qq", "10001", "小明")
    registry.ensure("qq", "10001", "明明")
    assert PeopleRegistry(layout).get(pid).names == ["小明", "明明"]


def test_bind_merges_auto_created_person(layout):
    registry = PeopleRegistry(layout)
    registry.create("xiaoming", "小明")
    qq_pid = registry.ensure("qq", "10001", "小明")
    mc_pid = registry.ensure("minecraft", "Steve", "Steve")

    registry.bind("qq", "10001", "xiaoming")
    registry.bind("minecraft", "Steve", "xiaoming")

    reloaded = PeopleRegistry(layout)
    assert reloaded.resolve("qq", "10001") == "xiaoming"
    assert reloaded.resolve("minecraft", "Steve") == "xiaoming"
    # 旧事件里记的 person_id 仍能找到同一个人
    assert reloaded.canonical(qq_pid) == "xiaoming"
    assert reloaded.canonical(mc_pid) == "xiaoming"
    assert reloaded.get(mc_pid).person_id == "xiaoming"
    assert [p.person_id for p in reloaded.list()] == ["xiaoming"]
    assert "Steve" in reloaded.get("xiaoming").names


def test_bind_keeps_other_accounts(layout):
    registry = PeopleRegistry(layout)
    registry.create("a", "甲")
    registry.create("b", "乙")
    registry.bind("qq", "1", "a")
    registry.bind("qq", "2", "a")
    registry.bind("qq", "2", "b")
    assert registry.get("a").accounts == {"qq": ["1"]}
    assert registry.get("a").merged_into is None


def test_bind_to_unknown_person_fails(layout):
    with pytest.raises(PeopleError):
        PeopleRegistry(layout).bind("qq", "1", "nobody")


@pytest.mark.parametrize("bad", ["Xiang", "self", "中文", "", "a" * 33])
def test_invalid_person_id(layout, bad):
    with pytest.raises(PeopleError):
        PeopleRegistry(layout).create(bad, "名字")


def test_body_edits_survive_rewrites(layout):
    registry = PeopleRegistry(layout)
    pid = registry.ensure("qq", "1", "甲")
    path = layout.person_path(pid)
    path.write_text(path.read_text("utf-8") + "- 喜欢不加糖、加奶的咖啡\n", encoding="utf-8")

    registry = PeopleRegistry(layout)
    registry.ensure("qq", "1", "新昵称")
    assert "喜欢不加糖、加奶的咖啡" in path.read_text("utf-8")
