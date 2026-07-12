"""End-to-end gate: spawns the real hubspot-mcp-server over real stdio transport
(via the `mcp` client SDK) against an in-process fake HubSpot CRM API, and
drives all five tools through a real MCP client session.

Unlike tests/test_server.py (which calls tool functions directly against a
MockTransport-backed HubSpotClient), this exercises the actual process
boundary: FastMCP's stdio framing, the real console-script-equivalent entry
point (tests/integration/gate_entry.py), and the real HubSpotClient/httpx
stack talking real HTTP to a real (if fake) server socket.

It also proves the read-only guard end-to-end, not just at the client's unit
test boundary: after driving every tool, it asserts that the fake API never
received any request other than a GET or the one allowlisted POST to a
`/search` path — i.e. there is no way to reach a write-shaped request through
the tool surface as actually exposed over stdio.

Self-contained and credential-free: the fake CRM API is a thread inside this
test process (tests/integration/fake_hubspot_api.py), bound to an
OS-assigned port, so there's no fixed-port collision risk and nothing to
clean up beyond `stop()`. Fast (~1s) and runs by default in CI — no network,
no HubSpot portal required.
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_hubspot_api import FakeHubSpotAPI

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_ENTRY = Path(__file__).resolve().with_name("gate_entry.py")

EXPECTED_TOOLS = [
    "get_associations",
    "get_record",
    "list_properties",
    "list_records",
    "search_records",
]


async def _run_gate(fake: FakeHubSpotAPI) -> None:
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", str(GATE_ENTRY)],
        cwd=str(REPO_ROOT),
        # Scrub HUBSPOT_MCP_* so a developer's exported vars (or CI leftovers) can't
        # reach the spawned server; the gate owns the only HUBSPOT_MCP_* var it needs.
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("HUBSPOT_MCP_")},
            "HUBSPOT_MCP_GATE_FAKE_PORT": str(fake.port),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = sorted(t.name for t in (await session.list_tools()).tools)
            assert tools == EXPECTED_TOOLS

            checks = [
                (
                    "list_records",
                    {"object_type": "contacts"},
                    ["id: 101", "ann@example.com"],
                ),
                (
                    "get_record",
                    {"object_type": "contacts", "record_id": "101"},
                    ["id: 101", "Ann", "Lee"],
                ),
                (
                    "search_records",
                    {"object_type": "contacts", "query": "ann"},
                    ["id: 101", "ann@example.com"],
                ),
                (
                    "get_associations",
                    {
                        "object_type": "companies",
                        "record_id": "500",
                        "to_object_type": "contacts",
                    },
                    ["toObjectId: 101", "type 279"],
                ),
                (
                    "list_properties",
                    {"object_type": "contacts"},
                    ["email (string): Email"],
                ),
            ]
            for name, args, expect in checks:
                result = await session.call_tool(name, args)
                text = result.content[0].text
                for needle in expect:
                    assert needle in text, f"{name}: {needle!r} not in {text[:200]!r}"

    # The read-only guard, proven end-to-end: every request the fake API's do_GET/
    # do_POST handlers actually received while all five tools ran was a GET, except
    # exactly one POST — the allowlisted search_records call. (A PUT/PATCH/DELETE
    # couldn't even show up here — BaseHTTPRequestHandler answers those with a bare
    # 501 without ever reaching a handler that records the request — but that case
    # can't arise anyway: HubSpotClient defines no put/patch/delete method at all,
    # see client.py's module docstring, so there is no way for the tools above to
    # have issued one.)
    requests = fake.requests
    assert requests, "fake API received no requests at all — did the tools even run?"
    assert [r for r in requests if r[0] != "GET"] == [("POST", "/crm/v3/objects/contacts/search")]


def test_stdio_roundtrip_drives_all_five_tools_and_proves_read_only_guard():
    fake = FakeHubSpotAPI().start()
    try:
        asyncio.run(asyncio.wait_for(_run_gate(fake), timeout=30))
    finally:
        fake.stop()


if __name__ == "__main__":
    # Allows `uv run python tests/integration/test_stdio_roundtrip.py` for a quick
    # manual run outside pytest, matching the original gate script's ergonomics.
    test_stdio_roundtrip_drives_all_five_tools_and_proves_read_only_guard()
    print("GATE PASSED", file=sys.stderr)
