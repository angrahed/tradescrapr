import pytest

from tradescrapr.config import load_config
from tradescrapr.errors import ConfigError


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("COMTRADE_API_KEY", "COMTRADE_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_key_from_env_var(tmp_path, clean_env):
    clean_env.setenv("COMTRADE_API_KEY", "abc123")
    assert load_config(root=tmp_path).api_key == "abc123"


def test_key_legacy_name_still_accepted(tmp_path, clean_env):
    clean_env.setenv("COMTRADE_KEY", "legacy456")
    assert load_config(root=tmp_path).api_key == "legacy456"


def test_key_primary_name_wins(tmp_path, clean_env):
    clean_env.setenv("COMTRADE_API_KEY", "primary")
    clean_env.setenv("COMTRADE_KEY", "legacy")
    assert load_config(root=tmp_path).api_key == "primary"


def test_key_from_dotenv_file(tmp_path, clean_env):
    (tmp_path / ".env").write_text("COMTRADE_API_KEY=from-file\n")
    try:
        assert load_config(root=tmp_path).api_key == "from-file"
    finally:
        import os

        os.environ.pop("COMTRADE_API_KEY", None)  # load_dotenv mutates the process env


def test_missing_key_error_names_the_variable(tmp_path, clean_env):
    cfg = load_config(root=tmp_path)
    assert cfg.api_key is None
    with pytest.raises(ConfigError, match="COMTRADE_API_KEY"):
        cfg.require_key()
