import threading

import httpx
import pytest

from hubspot_mcp import server
from hubspot_mcp.client import HubSpotClient
from hubspot_mcp.settings import Settings

BASE_URL = "https://api.hubapi.com"


def _settings(**overrides) -> Settings:
    defaults = dict(
        access_token="tok-abc",
        base_url=BASE_URL,
        item_limit=25,
        timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _client(handler, settings=None) -> HubSpotClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HubSpotClient(settings or _settings(), http=http)


@pytest.fixture(autouse=True)
def _reset_server_state():
    """server._state is a module global — reset it around every test so one test's
    configure() can't leak into the next."""
    server._state = None
    yield
    server._state = None


# -- list_records ------------------------------------------------------------------


def test_list_records_sends_default_properties_for_known_object():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "1",
                        "properties": {
                            "email": "a@b.com",
                            "firstname": "Ann",
                            "lastname": "Lee",
                        },
                    }
                ],
                "paging": {},
            },
        )

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "properties=email%2Cfirstname%2Clastname" in captured["url"]
    assert "id: 1" in result
    assert "email: a@b.com" in result
    assert "firstname: Ann" in result


def test_list_records_unknown_object_sends_no_properties_param():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"results": [{"id": "1", "properties": {"custom_field": "x"}}], "paging": {}},
        )

    server.configure(_client(handler))

    result = server._list_records_sync("my_custom_object")

    assert "properties=" not in captured["url"]
    assert "custom_field: x" in result


def test_list_records_explicit_properties_override_default():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": [{"id": "1", "properties": {}}], "paging": {}})

    server.configure(_client(handler))

    server._list_records_sync("contacts", properties="hs_object_id, phone")

    assert "properties=hs_object_id%2Cphone" in captured["url"]


def test_list_records_skips_null_properties():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "1",
                        "properties": {
                            "email": "a@b.com",
                            "firstname": None,
                            "lastname": None,
                        },
                    }
                ],
                "paging": {},
            },
        )

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "email: a@b.com" in result
    assert "firstname" not in result
    assert "lastname" not in result


def test_list_records_follows_cursor_paging():
    calls = []

    def handler(request):
        after = request.url.params.get("after")
        calls.append(after)
        if after is None:
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "1", "properties": {}}],
                    "paging": {"next": {"after": "cursor-1"}},
                },
            )
        return httpx.Response(200, json={"results": [{"id": "2", "properties": {}}], "paging": {}})

    server.configure(_client(handler))

    result = server._list_records_sync("contacts", limit=2)

    assert calls == [None, "cursor-1"]
    assert "id: 1" in result
    assert "id: 2" in result


def test_list_records_more_available_note():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [{"id": "1", "properties": {}}],
                "paging": {"next": {"after": "cursor-1"}},
            },
        )

    server.configure(_client(handler))

    result = server._list_records_sync("contacts", limit=1)

    assert "more records may be available" in result


def test_list_records_no_results_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"results": [], "paging": {}})

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "No contacts records found" in result


def test_list_records_no_limit_arg_uses_configured_item_limit():
    """HUBSPOT_MCP_ITEM_LIMIT is the default page size when `limit` is omitted
    (regression: list_records used to hardcode `limit: int = 25`, leaving
    item_limit dead for this tool)."""
    captured = {}

    def handler(request):
        captured["limit"] = request.url.params["limit"]
        return httpx.Response(200, json={"results": [{"id": "1", "properties": {}}], "paging": {}})

    server.configure(_client(handler), item_limit=7)

    server._list_records_sync("contacts")

    assert captured["limit"] == "7"


def test_list_records_explicit_limit_overrides_item_limit():
    captured = {}

    def handler(request):
        captured["limit"] = request.url.params["limit"]
        return httpx.Response(200, json={"results": [{"id": "1", "properties": {}}], "paging": {}})

    server.configure(_client(handler), item_limit=7)

    server._list_records_sync("contacts", limit=3)

    assert captured["limit"] == "3"


def test_list_records_error_passthrough():
    def handler(request):
        return httpx.Response(401, json={"status": "error", "message": "bad token"})

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "Traceback" not in result
    assert "HubSpot rejected the token" in result


# -- get_record ----------------------------------------------------------------------


def test_get_record_happy_path():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "id": "42",
                "properties": {"email": "a@b.com", "firstname": "Ann", "lastname": "Lee"},
            },
        )

    server.configure(_client(handler))

    result = server._get_record_sync("contacts", "42")

    assert "properties=email%2Cfirstname%2Clastname" in captured["url"]
    assert "id: 42" in result
    assert "email: a@b.com" in result


def test_get_record_rejects_non_numeric_id_without_calling_hubspot():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"id": "1", "properties": {}})

    server.configure(_client(handler))

    result = server._get_record_sync("contacts", "not-a-number")

    assert calls["n"] == 0
    assert "doesn't look like a HubSpot record id" in result


def test_get_record_error_passthrough():
    def handler(request):
        return httpx.Response(404, json={"status": "error", "message": "not found"})

    server.configure(_client(handler))

    result = server._get_record_sync("contacts", "999")

    assert "Traceback" not in result
    assert "No contacts found with id 999" in result


# -- search_records --------------------------------------------------------------------


def test_search_records_sends_query_in_json_body_not_url():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"total": 1, "results": [{"id": "1", "properties": {}}]})

    server.configure(_client(handler))

    server._search_records_sync("contacts", "acme corp", limit=10)

    assert "acme" not in captured["url"]
    assert b'"query":"acme corp"' in captured["body"]
    assert b'"limit":10' in captured["body"]
    assert b'"email"' in captured["body"]  # default contacts properties sent


def test_search_records_showing_n_of_total():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "total": 50,
                "results": [{"id": str(i), "properties": {}} for i in range(5)],
            },
        )

    server.configure(_client(handler))

    result = server._search_records_sync("contacts", "acme", limit=5)

    assert "showing 5 of 50 matches" in result


def test_search_records_no_limit_arg_uses_configured_item_limit():
    """Same item_limit-governs-page-size fix as list_records, for search_records's
    search-body `limit` field."""
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json={"total": 1, "results": [{"id": "1", "properties": {}}]})

    server.configure(_client(handler), item_limit=7)

    server._search_records_sync("contacts", "acme")

    assert b'"limit":7' in captured["body"]


def test_search_records_explicit_limit_overrides_item_limit():
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json={"total": 1, "results": [{"id": "1", "properties": {}}]})

    server.configure(_client(handler), item_limit=7)

    server._search_records_sync("contacts", "acme", limit=3)

    assert b'"limit":3' in captured["body"]


def test_search_records_no_matches_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"total": 0, "results": []})

    server.configure(_client(handler))

    result = server._search_records_sync("contacts", "nope")

    assert "No contacts records matched" in result


def test_search_records_error_passthrough():
    def handler(request):
        return httpx.Response(500, json={"status": "error", "message": "boom"})

    server.configure(_client(handler))

    result = server._search_records_sync("contacts", "acme")

    assert "Traceback" not in result
    assert "HubSpot returned HTTP 500" in result


# -- get_associations --------------------------------------------------------------------


def test_get_associations_extracts_ids_and_labels():
    def handler(request):
        assert str(request.url).endswith(
            "/crm/v4/objects/companies/100/associations/contacts"
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "toObjectId": 78157755542,
                        "associationTypes": [
                            {"category": "USER_DEFINED", "typeId": 1, "label": "Co Worker"},
                            {"category": "HUBSPOT_DEFINED", "typeId": 449, "label": None},
                        ],
                    }
                ]
            },
        )

    server.configure(_client(handler))

    result = server._get_associations_sync("companies", "100", "contacts")

    assert "toObjectId: 78157755542" in result
    assert "Co Worker" in result
    assert "type 449" in result


def test_get_associations_rejects_non_numeric_id_without_calling_hubspot():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    server.configure(_client(handler))

    result = server._get_associations_sync("companies", "not-a-number", "contacts")

    assert calls["n"] == 0
    assert "doesn't look like a HubSpot record id" in result


def test_get_associations_no_results_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"results": []})

    server.configure(_client(handler))

    result = server._get_associations_sync("companies", "100", "contacts")

    assert "No contacts associations found for companies 100" in result


def test_get_associations_more_available_note():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [{"toObjectId": 1, "associationTypes": []}],
                "paging": {"next": {"after": "cursor-1"}},
            },
        )

    server.configure(_client(handler))

    result = server._get_associations_sync("companies", "100", "contacts")

    assert "more associations may be available" in result


def test_get_associations_error_passthrough():
    def handler(request):
        return httpx.Response(403, json={"status": "error", "message": "no scope"})

    server.configure(_client(handler))

    result = server._get_associations_sync("companies", "100", "contacts")

    assert "Traceback" not in result
    assert "lacks the scope" in result


# -- list_properties --------------------------------------------------------------------


def test_list_properties_formats_name_type_label():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "email", "type": "string", "label": "Email"},
                    {"name": "amount", "type": "number", "label": "Amount"},
                ]
            },
        )

    server.configure(_client(handler))

    result = server._list_properties_sync("contacts")

    assert "email (string): Email" in result
    assert "amount (number): Amount" in result


def test_list_properties_caps_at_item_limit_times_ten_with_note():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": f"prop_{i}", "type": "string", "label": f"Prop {i}"}
                    for i in range(30)
                ]
            },
        )

    server.configure(_client(handler), item_limit=2)
    # item_limit is only carried by _State, not the client itself, here — configure()
    # takes it as a separate arg (see below); this call uses the 2-arg helper directly.

    result = server._list_properties_sync("contacts")

    assert result.count(" (string): ") == 20
    assert "capped at 20" in result
    assert "10 more properties not shown" in result


def test_list_properties_no_properties_friendly_message():
    def handler(request):
        return httpx.Response(200, json={"results": []})

    server.configure(_client(handler))

    result = server._list_properties_sync("contacts")

    assert "No properties found" in result


def test_list_properties_error_passthrough():
    def handler(request):
        return httpx.Response(404, json={"status": "error", "message": "no such object"})

    server.configure(_client(handler))

    result = server._list_properties_sync("bogus_object")

    assert "Traceback" not in result


# -- usage_note ---------------------------------------------------------------------


def test_usage_note_appended_at_threshold():
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "50",
            },
            json={"results": [{"id": "1", "properties": {}}], "paging": {}},
        )

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "daily API limit usage is high" in result
    assert "50 of 1000" in result


def test_usage_note_absent_below_threshold():
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "900",
            },
            json={"results": [{"id": "1", "properties": {}}], "paging": {}},
        )

    server.configure(_client(handler))

    result = server._list_records_sync("contacts")

    assert "daily API limit" not in result


# -- async offload -------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    # Only asyncio is a dependency here (trio isn't installed) — pin the anyio pytest
    # plugin's backend parametrization to the one we actually run under.
    return "asyncio"


@pytest.mark.anyio
async def test_list_records_tool_wrapper_offloads_to_worker_thread():
    """The registered `list_records` tool is `async def` and must actually await its
    sync body via anyio.to_thread.run_sync rather than blocking the event loop — call
    the real wrapper (not `_list_records_sync`) from inside a running loop and confirm
    the sync body ran on a different thread than the test itself."""
    caller_thread = threading.get_ident()
    seen_thread = {}

    def handler(request):
        seen_thread["id"] = threading.get_ident()
        return httpx.Response(200, json={"results": [{"id": "1", "properties": {}}], "paging": {}})

    server.configure(_client(handler))

    result = await server.list_records("contacts")

    assert "id: 1" in result
    assert seen_thread["id"] != caller_thread


# -- main() ---------------------------------------------------------------------------


def test_main_missing_access_token_exits_cleanly_with_actionable_sentence(monkeypatch, capsys):
    """Settings()'s ValueError (missing token) must not surface as a traceback from
    main() — a clean stderr sentence and exit(1) instead."""
    monkeypatch.delenv("HUBSPOT_MCP_ACCESS_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        server.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "HUBSPOT_MCP_ACCESS_TOKEN" in captured.err
    assert "Traceback" not in captured.err


def test_error_result_still_appends_usage_note_when_quota_low():
    """A 403 arriving while daily usage is high must still carry the usage warning —
    the moment the caller most needs it (regression: error branch bypassed _finish)."""
    responses = iter([
        httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "40",
            },
            json={"results": [{"id": "1", "properties": {"email": "a@b.com"}}]},
        ),
        httpx.Response(
            403,
            json={"status": "error", "message": "scope missing", "category": "MISSING_SCOPES"},
        ),
    ])
    server.configure(_client(lambda request: next(responses)))

    server._list_records_sync("contacts")  # primes the low-quota state
    result = server._search_records_sync("contacts", "acme")  # errors, but must warn

    assert "40" in result and "1000" in result


def test_search_records_rejects_object_type_with_fragment():
    """object_type carrying `#`/`?`/`/` is rejected before any request — the fragment
    bypass that would route a search POST to a write endpoint (regression)."""
    sent = []
    def handler(request):
        sent.append((request.method, request.url.path))
        return httpx.Response(200, json={"total": 0, "results": []})
    server.configure(_client(handler))
    for bad in ["contacts#", "contacts?x=", "contacts/../foo", "conta cts"]:
        result = server._search_records_sync(bad, "acme")
        assert "Invalid object type" in result
    assert sent == []  # nothing ever reached the wire
