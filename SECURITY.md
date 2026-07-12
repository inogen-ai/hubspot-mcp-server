# Security Policy

## Supported versions

hubspot-mcp-server is pre-1.0. Only the latest commit on `main` and the most
recent tagged release are supported with security fixes. There is no
long-term-support branch.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories: open the
repo's **Security** tab and use **"Report a vulnerability"**. Do not open a
public issue for anything that could be exploited before a fix ships.

Include what you'd include in a bug report — affected version/commit,
reproduction steps, and impact. We'll acknowledge new reports within a few
business days and follow up with a plan or fix timeline.

## Scope

hubspot-mcp-server is a local stdio MCP server: it runs on your machine,
launched by your MCP client, and talks only to the HubSpot API at
`HUBSPOT_MCP_BASE_URL`. There is no network listener and no multi-user
surface — the deployment environment (the machine it runs on, and the MCP
client that launches it) is yours to secure.

The credential lives in one environment variable:
`HUBSPOT_MCP_ACCESS_TOKEN`, a HubSpot Private App access token. It belongs in
your environment or a secrets manager — never commit it, and never put it in
a `.env` file that gets checked in. Anyone with read access to your
environment or MCP client config effectively holds whatever access that
token's scopes grant; treat it accordingly.

**The server reads whatever the token's granted scopes allow.** HubSpot's own
Private App scope model is the actual access-control boundary — this server
enforces nothing beyond what the token is permitted to see, and does not
attempt to narrow that further. That the server has no write, update, or
delete capability anywhere in its HTTP client is a documented property (see
the README) — not a substitute for granting the token only the read scopes
it needs.
