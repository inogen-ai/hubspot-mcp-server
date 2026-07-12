"""Runtime configuration for hubspot-mcp-server, loaded from HUBSPOT_MCP_-prefixed
environment variables (or a .env file). `Settings()` validates eagerly, at
construction time, so a misconfigured server fails fast at startup with a clear,
actionable message rather than failing obscurely on the first tool call: a Private App
access token is configured, and `base_url` is an https origin HubSpot will actually
talk to (localhost is allowed too, for pointing the client at a local test double).
"""

from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUBSPOT_MCP_", env_file=".env")

    access_token: str = ""
    base_url: str = "https://api.hubapi.com"
    item_limit: int = 25
    timeout_seconds: float = 30.0

    @model_validator(mode="after")
    def _validate_access_token(self) -> "Settings":
        if not self.access_token:
            raise ValueError(
                "HUBSPOT_MCP_ACCESS_TOKEN must be set — create a Private App in your "
                "HubSpot portal (Settings > Integrations > Private Apps), grant it "
                "read scopes, and copy its access token."
            )
        return self

    @model_validator(mode="after")
    def _validate_base_url(self) -> "Settings":
        parts = urlsplit(self.base_url)
        is_localhost = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (parts.scheme == "http" and is_localhost):
            raise ValueError(
                f"HUBSPOT_MCP_BASE_URL must be https (got {self.base_url!r}) — "
                "HubSpot's API is always https. A plain-http localhost override is "
                "allowed for pointing the client at a local test double."
            )
        if not parts.netloc:
            raise ValueError(
                f"HUBSPOT_MCP_BASE_URL is not a valid URL: {self.base_url!r}"
            )
        # Normalize away any trailing slash so downstream URL-joining never produces
        # a doubled slash.
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        return self
