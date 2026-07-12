"""FastMCP server exposing five read-only HubSpot CRM tools over stdio (plan.md's
Tools table). Tools are thin: parse args, call the injected HubSpotClient, format
compact text — no raw HubSpot JSON ever reaches an MCP client. Every tool catches
`HubSpotError` (the only exception type the client's contract raises) and returns its
`.message` as the tool result rather than letting it propagate as a traceback. Every
non-empty tool result carries `client.usage_note()` as a trailing line once HubSpot's
daily API usage crosses 90% (see client.py).

Tests swap in a MockTransport-backed HubSpotClient via `configure()` — the module-level
`_state` it sets is what every tool function reads through `_get_state()`, so nothing
here talks to a real HubSpotClient directly. This mirrors m365-mcp-server's server.py
shape, including the async-offload pattern: every registered tool is `async def` and
awaits its sync `_*_sync` body via `anyio.to_thread.run_sync` so a slow HubSpot request
never blocks the event loop. HubSpot's Private App bearer auth needs no device-code
dance (unlike m365/sfdc's OAuth flows), so there is no LoginRequired/completion-thread
machinery here — HubSpotError is the only exception this module ever handles.

HubSpot's object model: `object_type` accepts a standard object name (contacts,
companies, deals, tickets) or a custom object's name/id; record ids are always numeric
strings; a full-text `search_records` query is plain text, never filter syntax.

Response shapes below (list envelope `results`/`paging.next.after`, `properties` on
each record, and the v4 associations `results[].toObjectId` /
`results[].associationTypes[].{category,typeId,label}` envelope) were verified against
HubSpot's docs and public examples (2026-07-12) rather than recalled from memory:
https://developers.hubspot.com/docs/api-reference/legacy/crm/associations/associate-records/get-associations
(v4 associations GET response: `results[].toObjectId` + `results[].associationTypes[]`
with `category`/`typeId`/`label`, `label` is `null` for HUBSPOT_DEFINED types) and the
contacts list envelope (`results[].{id,properties,createdAt,updatedAt,archived}` +
`paging.next.after`), corroborated via HubSpot community examples since the primary
`/docs/api-reference/crm-contacts-v3/guide` and `/crm-associations-v4/guide` pages
redirect to a login-gated `app.hubspot.com` host and can't be fetched directly (same
gate noted in Task 1's client.py docstring).
"""

import re
import sys
from functools import partial

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from hubspot_mcp.client import MAX_PAGE_SIZE, HubSpotClient, HubSpotError
from hubspot_mcp.settings import Settings

mcp = FastMCP("hubspot")

# Sensible default `properties` per standard object, used whenever the caller doesn't
# pass an explicit `properties` override — HubSpot's objects are property-sparse by
# default (a plain GET returns almost nothing useful without an explicit properties
# list). A custom object (or any name not in this map) gets NO properties param at
# all, so HubSpot falls back to its own default property set for that object rather
# than the request coming back empty.
DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "contacts": ["email", "firstname", "lastname"],
    "companies": ["name", "domain"],
    "deals": ["dealname", "amount", "dealstage"],
    "tickets": ["subject", "hs_pipeline_stage"],
}

# list_properties's field-list cap — schema-discovery only, but a heavily customized
# portal can have hundreds of properties on one object; a fixed multiple of item_limit
# keeps the listing a useful skim rather than a multi-hundred-line wall of text.
_PROPERTIES_CAP_MULTIPLIER = 10


def _split_csv(value: str) -> list[str]:
    """Comma-separated list -> stripped, non-empty entries. "" -> []."""
    return [item.strip() for item in value.split(",") if item.strip()]


def _resolve_properties(object_type: str, properties: str) -> list[str]:
    """An explicit `properties` override always wins; otherwise fall back to this
    object's default list (empty for a custom/unrecognized object_type, which means
    "send no properties param at all" — see DEFAULT_PROPERTIES)."""
    explicit = _split_csv(properties)
    if explicit:
        return explicit
    return DEFAULT_PROPERTIES.get(object_type, [])


def _valid_record_id(record_id: str) -> bool:
    """HubSpot record ids are plain ASCII-numeric strings (e.g. "12345") — never
    alphanumeric like a Salesforce Id. `.isdigit()` alone would admit non-ASCII digits
    (Arabic-Indic, superscripts), so require `.isascii()` too; both reject the empty
    string."""
    return record_id.isascii() and record_id.isdigit()


_OBJECT_TYPE_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def _valid_object_type(object_type: str) -> bool:
    """HubSpot object types are plain identifiers — standard names (contacts, deals),
    custom-object internal names (e.g. p_my_object), or object-type ids (e.g. 2-3801918).
    Reject anything else: a `#`, `?`, `/`, or whitespace in an object_type flows into
    a request path, and a fragment/query would let a POST that string-ends in `/search`
    resolve, once httpx strips it, to a non-search (write) endpoint — so this guard is
    part of the read-only guarantee, not just input hygiene."""
    return bool(_OBJECT_TYPE_RE.match(object_type))


def _format_record_block(record: dict, properties: list[str]) -> str:
    """One record as `id` first, then the requested properties (or every property
    HubSpot returned, when `properties` is empty — the custom-object/no-default-props
    case) as `label: value` lines. A property HubSpot reports as null is skipped
    entirely rather than printed as `key: None` — HubSpot's properties are sparse by
    design, and a wall of `key: None` lines would swamp the properties that actually
    have data."""
    lines = [f"id: {record.get('id', '')}"]
    props = record.get("properties") or {}
    keys = properties if properties else list(props.keys())
    for key in keys:
        value = props.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


class _State:
    """Bundles the HubSpotClient tools call with the configured item_limit
    (list_properties' cap is a multiple of it; HubSpotClient keeps its own copy
    privately, so the server tracks its own rather than reaching into the client)."""

    def __init__(self, client: HubSpotClient, item_limit: int):
        self.client = client
        self.item_limit = item_limit


_state: _State | None = None


def configure(client: HubSpotClient, item_limit: int = 25) -> None:
    """Inject the HubSpotClient the tools operate on. Called once by `main()` at
    startup, and by tests to swap in a MockTransport-backed fake."""
    global _state
    _state = _State(client, item_limit)


def _get_state() -> _State:
    if _state is None:
        raise RuntimeError("hubspot_mcp.server.configure() must be called before any tool runs.")
    return _state


def _finish(text: str) -> str:
    """Append client.usage_note() as a trailing line when HubSpot's daily API usage has
    crossed 90% (see client.py) — every tool's final formatting step."""
    note = _get_state().client.usage_note()
    if note is None:
        return text
    return f"{text}\n\n{note}"


# -- list_records ----------------------------------------------------------------------


def _list_records_sync(
    object_type: str, properties: str = "", limit: int | None = None
) -> str:
    if not _valid_object_type(object_type):
        return (
            f"Invalid object type {object_type!r} — use a HubSpot object name "
            "(contacts, companies, deals, tickets), a custom object's internal "
            "name, or an object-type id."
        )
    props = _resolve_properties(object_type, properties)
    params: dict[str, str] = {}
    if props:
        params["properties"] = ",".join(props)

    limit = _get_state().item_limit if limit is None else limit
    try:
        records, after = _get_state().client.query_paged(
            f"/crm/v3/objects/{object_type}", params=params, limit=limit
        )
    except HubSpotError as exc:
        return _finish(str(exc))

    if not records:
        return _finish(f"No {object_type} records found.")
    blocks = [_format_record_block(record, props) for record in records]
    result = "\n\n".join(blocks)
    if after is not None:
        # Plain list endpoints never report a `total` — an `after` cursor left over is
        # the only "more available" signal there is (contrast search_records, whose
        # response carries an actual total).
        result += "\n\n... results capped; more records may be available."
    return _finish(result)


@mcp.tool()
async def list_records(
    object_type: str, properties: str = "", limit: int | None = None
) -> str:
    """List recent records of `object_type` (contacts, companies, deals, tickets, or a
    custom object's name). `properties` is an optional comma-separated list of property
    names to return (default: a sensible per-object set for the four standard objects;
    HubSpot's own default properties for anything else). Returns up to `limit` records
    (default: HUBSPOT_MCP_ITEM_LIMIT), each block starting with its numeric `id`."""
    return await anyio.to_thread.run_sync(
        partial(_list_records_sync, object_type, properties, limit)
    )


# -- get_record ------------------------------------------------------------------------


def _get_record_sync(object_type: str, record_id: str, properties: str = "") -> str:
    if not _valid_object_type(object_type):
        return (
            f"Invalid object type {object_type!r} — use a HubSpot object name "
            "(contacts, companies, deals, tickets), a custom object's internal "
            "name, or an object-type id."
        )
    if not _valid_record_id(record_id):
        return (
            f"{record_id!r} doesn't look like a HubSpot record id — HubSpot ids are "
            "plain numeric strings (e.g. \"12345\"). Check the value and try again."
        )

    props = _resolve_properties(object_type, properties)
    params = {"properties": ",".join(props)} if props else None
    try:
        record = _get_state().client.get(
            f"/crm/v3/objects/{object_type}/{record_id}", params=params
        )
    except HubSpotError as exc:
        return _finish(str(exc))

    if not isinstance(record, dict):
        return "HubSpot returned an unexpected response shape for this record."
    return _finish(_format_record_block(record, props))


@mcp.tool()
async def get_record(object_type: str, record_id: str, properties: str = "") -> str:
    """Fetch one record by numeric id from `object_type` (contacts, companies, deals,
    tickets, or a custom object's name). `properties` is an optional comma-separated
    list of property names to return (default: the same per-object set list_records
    uses). To find record ids first: list_records or search_records."""
    return await anyio.to_thread.run_sync(
        partial(_get_record_sync, object_type, record_id, properties)
    )


# -- search_records --------------------------------------------------------------------


def _search_records_sync(object_type: str, query: str, limit: int | None = None) -> str:
    if not _valid_object_type(object_type):
        return (
            f"Invalid object type {object_type!r} — use a HubSpot object name "
            "(contacts, companies, deals, tickets), a custom object's internal "
            "name, or an object-type id."
        )
    limit = _get_state().item_limit if limit is None else limit
    props = _resolve_properties(object_type, "")
    body: dict[str, object] = {"query": query, "limit": min(limit, MAX_PAGE_SIZE)}
    if props:
        body["properties"] = props

    try:
        result = _get_state().client.post(f"/crm/v3/objects/{object_type}/search", json=body)
    except HubSpotError as exc:
        return _finish(str(exc))

    if not isinstance(result, dict):
        return "HubSpot returned an unexpected response shape for this search."

    records = (result.get("results") or [])[:limit]
    total = result.get("total")
    if not records:
        return _finish(f"No {object_type} records matched {query!r}.")

    blocks = [_format_record_block(record, props) for record in records]
    result_text = "\n\n".join(blocks)
    shown = len(records)
    if isinstance(total, int) and total > shown:
        result_text += f"\n\n(showing {shown} of {total} matches)"
    return _finish(result_text)


@mcp.tool()
async def search_records(
    object_type: str, query: str, limit: int | None = None
) -> str:
    """Full-text search `object_type` (contacts, companies, deals, tickets, or a custom
    object's name) for `query` — plain search text (e.g. a name, email, or company
    domain), never filter/query syntax; it goes straight into the search request's JSON
    `query` field, not string-interpolated into a URL or filter expression. Returns up
    to `limit` matches (default: HUBSPOT_MCP_ITEM_LIMIT), each block starting with its
    numeric `id`."""
    return await anyio.to_thread.run_sync(
        partial(_search_records_sync, object_type, query, limit)
    )


# -- get_associations --------------------------------------------------------------------


def _format_association(entry: dict) -> str:
    to_id = entry.get("toObjectId", "")
    types = entry.get("associationTypes") or []
    # `label` is `null` on HUBSPOT_DEFINED association types (per HubSpot's v4
    # response) — fall back to a "type <id>" tag rather than rendering a bare "None".
    labels = [t.get("label") or f"type {t.get('typeId', '?')}" for t in types]
    type_text = ", ".join(labels) if labels else "(no association type reported)"
    return f"- toObjectId: {to_id}\n  associationTypes: {type_text}"


def _get_associations_sync(object_type: str, record_id: str, to_object_type: str) -> str:
    if not _valid_object_type(object_type):
        return (
            f"Invalid object type {object_type!r} — use a HubSpot object name "
            "(contacts, companies, deals, tickets), a custom object's internal "
            "name, or an object-type id."
        )
    if not _valid_record_id(record_id):
        return (
            f"{record_id!r} doesn't look like a HubSpot record id — HubSpot ids are "
            "plain numeric strings (e.g. \"12345\"). Check the value and try again."
        )

    try:
        body = _get_state().client.get(
            f"/crm/v4/objects/{object_type}/{record_id}/associations/{to_object_type}"
        )
    except HubSpotError as exc:
        return _finish(str(exc))

    if not isinstance(body, dict):
        return "HubSpot returned an unexpected response shape for these associations."

    results = body.get("results") or []
    if not results:
        return _finish(
            f"No {to_object_type} associations found for {object_type} {record_id}."
        )
    result_text = "\n".join(_format_association(entry) for entry in results)
    paging = body.get("paging")
    if isinstance(paging, dict) and isinstance(paging.get("next"), dict):
        result_text += "\n\n... results capped; more associations may be available."
    return _finish(result_text)


@mcp.tool()
async def get_associations(object_type: str, record_id: str, to_object_type: str) -> str:
    """List the `to_object_type` records associated with one `object_type` record (e.g.
    the contacts associated with a company) — the CRM relationship graph. Returns each
    related record's numeric id (`toObjectId`, usable directly with get_record) plus its
    association type label(s)."""
    return await anyio.to_thread.run_sync(
        partial(_get_associations_sync, object_type, record_id, to_object_type)
    )


# -- list_properties --------------------------------------------------------------------


def _format_property(prop: dict) -> str:
    name = prop.get("name", "?")
    prop_type = prop.get("type", "?")
    label = prop.get("label", "")
    return f"{name} ({prop_type}): {label}"


def _list_properties_sync(object_type: str) -> str:
    if not _valid_object_type(object_type):
        return (
            f"Invalid object type {object_type!r} — use a HubSpot object name "
            "(contacts, companies, deals, tickets), a custom object's internal "
            "name, or an object-type id."
        )
    try:
        body = _get_state().client.get(f"/crm/v3/properties/{object_type}")
    except HubSpotError as exc:
        return _finish(str(exc))

    if not isinstance(body, dict):
        return "HubSpot returned an unexpected response shape for this object's schema."

    props = body.get("results") or []
    cap = _get_state().item_limit * _PROPERTIES_CAP_MULTIPLIER
    shown = props[:cap]
    if not shown:
        return _finish(f"No properties found for {object_type!r} — check the object_type name.")

    lines = [_format_property(prop) for prop in shown]
    if len(props) > cap:
        lines.append(
            f"... {len(props) - cap} more properties not shown (capped at {cap} — "
            "heavily customized portals can have hundreds of properties per object)."
        )
    return _finish("\n".join(lines))


@mcp.tool()
async def list_properties(object_type: str) -> str:
    """List the properties (schema) defined on `object_type` (contacts, companies,
    deals, tickets, or a custom object's name) — each as `name (type): label`. Use this
    to discover valid property names before building a list_records/get_record/
    search_records `properties` list."""
    return await anyio.to_thread.run_sync(partial(_list_properties_sync, object_type))


def main() -> None:
    """Console-script entry point: build settings/client from the environment and run
    the MCP server over stdio (the standard transport for local MCP servers)."""
    try:
        settings = Settings()
    except ValueError as exc:
        # Settings' ValueError messages are already actionable sentences naming the
        # env var to fix — surface exactly that on stderr, not a traceback, and exit
        # non-zero so a process supervisor sees a clean failure.
        print(f"hubspot-mcp-server: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    client = HubSpotClient(settings)
    configure(client, settings.item_limit)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
