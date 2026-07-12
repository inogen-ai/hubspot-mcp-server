"""Stdio entry point for the integration gate (test_stdio_roundtrip.py).

Spawned by the test as a real subprocess (`uv run python tests/integration/
gate_entry.py`) over the mcp client SDK's stdio transport — this is the real
FastMCP server (`hubspot_mcp.server`) wired to a real `HubSpotClient`, built
exactly the way `hubspot_mcp.server.main()` builds it. Unlike snow-mcp-server's
gate (which needs a post-construction attribute swap because its
`instance_url` validator has no test-double escape hatch), HubSpotClient's
`Settings.base_url` validator explicitly allows a plain-http `127.0.0.1`
origin "for pointing the client at a local test double" (see settings.py's
docstring) — so this entry point builds `Settings` straight from that
allowance, with no attribute-swap seam at all, and could be `main()` itself
except for one thing: `_env_file=None`.

Settings' default `env_file=".env"` means a plain `Settings()` call — what
`main()` does — would let a repo-root `.env` bleed into the gate if one ever
exists (a maintainer's real `HUBSPOT_MCP_ACCESS_TOKEN` for local manual
verification, say). `_env_file=None` here guarantees the gate only ever sees
the two settings it constructs explicitly, never anything a developer's
working tree happens to have lying around.

The fake API's port is passed in via the HUBSPOT_MCP_GATE_FAKE_PORT
environment variable rather than hardcoded, since
tests/integration/fake_hubspot_api.py binds an OS-assigned port (port 0) to
avoid fixed-port collisions.
"""

import os

from hubspot_mcp import server
from hubspot_mcp.client import HubSpotClient
from hubspot_mcp.settings import Settings

port = os.environ["HUBSPOT_MCP_GATE_FAKE_PORT"]

settings = Settings(
    _env_file=None,  # a repo-root .env must not bleed into the gate
    access_token="gate-stub-token",
    base_url=f"http://127.0.0.1:{port}",
)
client = HubSpotClient(settings)
server.configure(client, settings.item_limit)
server.mcp.run(transport="stdio")
