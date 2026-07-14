# hubspot-mcp-server

[![CI](https://img.shields.io/github/actions/workflow/status/inogen-ai/hubspot-mcp-server/ci.yml?branch=main&label=CI)](https://github.com/inogen-ai/hubspot-mcp-server/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

![hubspot-mcp-server driving read-only HubSpot CRM tools over MCP](docs/assets/demo.gif)

A production-grade, **read-only** MCP server for HubSpot CRM: list and fetch contacts,
companies, deals, and tickets (or any custom object), full-text search, walk the CRM's
association graph, and discover an object's property schema — from Claude Desktop,
Claude Code, or any MCP client — with HubSpot's rate limits handled properly and no
live portal required to run the test suite.

**Read-only by construction:** the HTTP layer this server is built on
(`hubspot_mcp.client.HubSpotClient`) exposes exactly two verbs — `get`, and `post`. The
only `post` calls this server ever makes are to HubSpot's CRM Search API
(`POST /crm/v3/objects/{object}/search`), which HubSpot itself models as a POST purely
because the search filter body is too large for a query string — it is a read, not a
write. The client hard-allowlists this: `post()` rejects any path that doesn't end in
`/search` with a `ValueError` before a request is ever sent. There is no `put`,
`patch`, or `delete` method anywhere in the codebase.

**The server can read whatever the token's scopes allow.** A HubSpot Private App
access token's granted scopes are the actual security boundary — this server enforces
nothing beyond what the token itself is permitted to see. **Grant only the read scopes
you need** (e.g. `crm.objects.contacts.read`, not every `crm.objects.*.read` scope) —
see the setup walkthrough below.

`hubspot-mcp-server` is not affiliated with, endorsed by, or sponsored by HubSpot, Inc.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and a HubSpot portal to point at. Don't have
one? [developers.hubspot.com](https://developers.hubspot.com/) lets you create a free
developer account and a test portal in a couple of minutes — no cost, no credit card.

### Install and run

This project is published to PyPI as the distribution `hubspot-mcp`; its console
script is named `hubspot-mcp-server` (a different, unrelated package already holds the
`hubspot-mcp-server` PyPI name). Because of that mismatch, you must tell
`uvx` which distribution to pull the script from — `uvx hubspot-mcp-server` alone would
fetch the *wrong* package:

    uvx --from hubspot-mcp hubspot-mcp-server

That starts the server over stdio. In practice you'll point an MCP client at it
instead of running it directly — for Claude Code:

    claude mcp add hubspot \
      -e HUBSPOT_MCP_ACCESS_TOKEN=<your-private-app-token> \
      -- uvx --from hubspot-mcp hubspot-mcp-server

For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hubspot": {
      "command": "uvx",
      "args": ["--from", "hubspot-mcp", "hubspot-mcp-server"],
      "env": {
        "HUBSPOT_MCP_ACCESS_TOKEN": "<your-private-app-token>"
      }
    }
  }
}
```

## Create a Private App and access token

1. In your HubSpot portal, click the **Settings** gear icon (top nav).
2. In the left sidebar, go to **Integrations > Private Apps**.
3. Click **Create a private app**, give it a name (e.g. "hubspot-mcp-server").
4. Open the **Scopes** tab and select the *read* scopes for what you want this server
   to access — for example `crm.objects.contacts.read`,
   `crm.objects.companies.read`, `crm.objects.deals.read`,
   `crm.objects.tickets.read`, and `crm.schemas.contacts.read` (and the matching
   `crm.schemas.<object>.read` scopes for whichever objects you use `list_properties`
   on). Grant only what you actually need — see the scope warning above.
5. Click **Create app**, confirm, then **copy the access token** shown — HubSpot only
   displays it once. Store it somewhere safe; it's a secret.

## Tools

| Tool | Parameters | Returns |
|---|---|---|
| `list_records` | `object_type: str`, `properties: str = ""`, `limit: int \| None = None` | Recent records of `object_type`. Sensible default properties per standard object (see below); `properties` (comma-separated) overrides them. `id` is always first. Says plainly when more records are available than `limit` returned. `limit` defaults to `HUBSPOT_MCP_ITEM_LIMIT` when omitted. |
| `get_record` | `object_type: str`, `record_id: str`, `properties: str = ""` | One full record by numeric id — typically an `id` from a prior `list_records`/`search_records` call. |
| `search_records` | `object_type: str`, `query: str`, `limit: int \| None = None` | Full-text search of `object_type` for `query` (plain text — a name, email, domain, etc. — never filter syntax) via the CRM Search API. Appends "(showing N of total matches)" when more matches exist than `limit` returned. `limit` defaults to `HUBSPOT_MCP_ITEM_LIMIT` when omitted. |
| `get_associations` | `object_type: str`, `record_id: str`, `to_object_type: str` | The `to_object_type` records associated with one `object_type` record (e.g. the contacts on a company) — the CRM relationship graph. Each hit's numeric id composes directly into `get_record`. |
| `list_properties` | `object_type: str` | The property schema (name, type, label) defined on `object_type` — discover valid property names before building a `properties` list. Capped at a multiple of `HUBSPOT_MCP_ITEM_LIMIT`, with a note when more exist. |

### Object model cheat sheet

`object_type` accepts a standard HubSpot object name — `contacts`, `companies`,
`deals`, `tickets` — or a custom object's name, everywhere it appears above. Record
ids are always plain numeric strings (e.g. `"12345"`), never HubSpot's `hs_object_id`
formatted any other way; `get_record` rejects anything that isn't purely digits before
making a request. `search_records`' `query` is plain search text — it goes straight
into the search request's JSON `query` field, never string-interpolated into a filter
expression.

## Environment variables

All settings are prefixed `HUBSPOT_MCP_` and can be set in the environment or a `.env`
file (see `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `HUBSPOT_MCP_ACCESS_TOKEN` | *(unset, required)* | Private App access token, sent as `Authorization: Bearer`. See the setup walkthrough above. |
| `HUBSPOT_MCP_BASE_URL` | `https://api.hubapi.com` | HubSpot API origin. Override for EU data residency or to point the client at a local test double; must be https (a plain-http `localhost` override is allowed for testing). |
| `HUBSPOT_MCP_ITEM_LIMIT` | `25` | Default page size for `list_records`/`search_records` when their `limit` parameter is omitted, and the base for `list_properties`'s output cap (`item_limit` × 10, see the Tools table). Pass a per-call `limit` to `list_records`/`search_records` to override it for that call. |
| `HUBSPOT_MCP_TIMEOUT_SECONDS` | `30.0` | HTTP timeout (seconds) per HubSpot request. |

`HUBSPOT_MCP_ACCESS_TOKEN` unset fails startup immediately with a message naming the
env var to fix, rather than failing obscurely on the first tool call.

## Rate limits

HubSpot Private Apps are subject to daily and per-second API call limits. This server
reads HubSpot's `X-HubSpot-RateLimit-*` headers off every response and appends a
warning to a tool's result once daily usage crosses 90% *used* (10% or less of the
daily quota remaining), so a client sees it coming rather than hitting a hard stop
mid-session. A `429` is retried honoring
`Retry-After` (clamped to at most 60s, up to 3 retries, exponential 1→2→4s backoff
when no header is sent) — except when HubSpot's error body names the `DAILY` policy,
which is not retried (retrying can't fix a quota that only resets at midnight portal
time) and instead returns an actionable message immediately.

## Security notes

- **Read-only by construction**, not by policy — see the warning at the top of this
  README. The Private App token's granted scopes, not this server, decide what's
  actually readable.
- **Rate-limit aware.** See "Rate limits" above.
- Credentials never appear in error messages, logs, or exceptions raised to an MCP
  client — failures are reduced to plain, credential-free sentences before they leave
  the client layer.
- Not affiliated with, endorsed by, or sponsored by HubSpot, Inc.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

    uv sync
    uv run pytest -q
    uv run ruff check .

No live HubSpot portal is needed for the test suite — the CRM API is faked at the
`httpx.MockTransport` boundary, and a committed stdio integration gate drives the real
server process against an in-process fake CRM API. See
[docs/manual-verification.md](docs/manual-verification.md) for the live-portal check a
maintainer runs before releases.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and PR
expectations, and [SECURITY.md](SECURITY.md) for reporting vulnerabilities privately.

---

Not affiliated with, endorsed by, or sponsored by HubSpot, Inc.

Part of [InoGen's open-source portfolio](https://github.com/inogen-ai): [kilnworks](https://github.com/inogen-ai/kilnworks) (self-hostable RAG assistant) and the read-only MCP connectors [m365](https://github.com/inogen-ai/m365-mcp-server), [servicenow](https://github.com/inogen-ai/snow-mcp-server), [salesforce](https://github.com/inogen-ai/sfdc-mcp-server), and [hubspot](https://github.com/inogen-ai/hubspot-mcp-server).

Built and maintained by [InoGen](https://inogen.ai).
