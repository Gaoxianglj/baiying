import pytest

from companion.config import ConfigError, load_config


def test_defaults_without_any_files(tmp_path):
    config = load_config(tmp_path, environ={})
    assert config.persona.name == "白璎"
    assert config.models.fallback.base_url == "https://api.deepseek.com"
    assert config.models.fallback.api_key is None
    assert config.memory_dir == tmp_path.resolve() / "data" / "memory"


def test_precedence_toml_local_dotenv_environ(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[models.fallback]\nmodel = "from-toml"\nmonthly_budget_usd = 5\n', encoding="utf-8"
    )
    (tmp_path / "config.local.toml").write_text(
        '[models.fallback]\nmodel = "from-local"\n', encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "FALLBACK_MODEL=from-dotenv\nQQ_BOT_ACCOUNT=10001\n", encoding="utf-8"
    )
    config = load_config(tmp_path, environ={"QQ_BOT_ACCOUNT": "10002"})
    assert config.models.fallback.model == "from-dotenv"
    assert config.models.fallback.monthly_budget_usd == 5
    assert config.qq.bot_account == "10002"


def test_empty_env_values_are_ignored(tmp_path):
    (tmp_path / "config.toml").write_text('[models.fallback]\nmodel = "m"\n', encoding="utf-8")
    config = load_config(tmp_path, environ={"FALLBACK_MODEL": ""})
    assert config.models.fallback.model == "m"


def test_api_key_is_masked_in_dump(tmp_path):
    config = load_config(tmp_path, environ={"FALLBACK_API_KEY": "sk-secret"})
    assert config.models.fallback.api_key.get_secret_value() == "sk-secret"
    assert "sk-secret" not in config.model_dump_json()


@pytest.mark.parametrize("name", ["config.toml", "config.local.toml"])
def test_api_key_in_toml_is_rejected(tmp_path, name):
    (tmp_path / name).write_text('[models.fallback]\napi_key = "sk-oops"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="FALLBACK_API_KEY"):
        load_config(tmp_path, environ={})


def test_unknown_key_is_rejected(tmp_path):
    (tmp_path / "config.toml").write_text("[persona]\nnmae = 'typo'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path, environ={})


def test_repo_config_toml_loads():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = load_config(root, environ={})
    assert config.persona.name == "白璎"
