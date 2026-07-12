import pytest

from hubspot_mcp.settings import Settings


def _env(monkeypatch, token="tok-abc", **extra):
    monkeypatch.setenv("HUBSPOT_MCP_ACCESS_TOKEN", token)
    for key, value in extra.items():
        monkeypatch.setenv(f"HUBSPOT_MCP_{key}", value)


# -- defaults / round trip -----------------------------------------------------------


def test_defaults(monkeypatch):
    _env(monkeypatch)
    settings = Settings()

    assert settings.access_token == "tok-abc"
    assert settings.base_url == "https://api.hubapi.com"
    assert settings.item_limit == 25
    assert settings.timeout_seconds == 30.0


def test_item_limit_and_timeout_overridable(monkeypatch):
    _env(monkeypatch, ITEM_LIMIT="5", TIMEOUT_SECONDS="10.5")

    settings = Settings()

    assert settings.item_limit == 5
    assert settings.timeout_seconds == 10.5


def test_unprefixed_env_vars_are_ignored(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("ACCESS_TOKEN", "should-not-be-picked-up")
    monkeypatch.setenv("ITEM_LIMIT", "999")

    settings = Settings()

    assert settings.access_token == "tok-abc"
    assert settings.item_limit == 25


# -- access_token validation ----------------------------------------------------------


def test_missing_access_token_raises_clear_value_error(monkeypatch):
    with pytest.raises(ValueError) as exc_info:
        Settings()

    message = str(exc_info.value)
    assert "HUBSPOT_MCP_ACCESS_TOKEN" in message


def test_empty_access_token_raises(monkeypatch):
    monkeypatch.setenv("HUBSPOT_MCP_ACCESS_TOKEN", "")

    with pytest.raises(ValueError) as exc_info:
        Settings()

    assert "HUBSPOT_MCP_ACCESS_TOKEN" in str(exc_info.value)


# -- base_url validation ---------------------------------------------------------------


def test_base_url_defaults_to_hubapi(monkeypatch):
    _env(monkeypatch)

    assert Settings().base_url == "https://api.hubapi.com"


def test_base_url_http_scheme_raises(monkeypatch):
    _env(monkeypatch, BASE_URL="http://api.hubapi.com")

    with pytest.raises(ValueError) as exc_info:
        Settings()

    assert "https" in str(exc_info.value)


def test_base_url_http_localhost_is_allowed(monkeypatch):
    _env(monkeypatch, BASE_URL="http://localhost:8000")

    settings = Settings()

    assert settings.base_url == "http://localhost:8000"


def test_base_url_http_127_0_0_1_is_allowed(monkeypatch):
    _env(monkeypatch, BASE_URL="http://127.0.0.1:8000")

    settings = Settings()

    assert settings.base_url == "http://127.0.0.1:8000"


def test_base_url_https_localhost_is_allowed(monkeypatch):
    _env(monkeypatch, BASE_URL="https://localhost:8443")

    settings = Settings()

    assert settings.base_url == "https://localhost:8443"


def test_base_url_not_a_url_raises(monkeypatch):
    _env(monkeypatch, BASE_URL="not-a-url")

    with pytest.raises(ValueError):
        Settings()


def test_base_url_trailing_slash_is_stripped(monkeypatch):
    _env(monkeypatch, BASE_URL="https://api.hubapi.com/")

    settings = Settings()

    assert settings.base_url == "https://api.hubapi.com"
