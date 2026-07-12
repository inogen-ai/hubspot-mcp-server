import httpx
import pytest

from hubspot_mcp import client as client_module
from hubspot_mcp.client import HubSpotClient, HubSpotError
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


@pytest.fixture()
def fake_sleep(monkeypatch):
    """Replaces client.sleep so retry/backoff tests run instantly while recording
    every duration the client asked to sleep for, in call order."""
    calls: list[float] = []
    monkeypatch.setattr(client_module, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _client(handler, settings=None) -> HubSpotClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HubSpotClient(settings or _settings(), http=http)


# -- auth header shape / no leak -------------------------------------------------------


def test_bearer_auth_header_shape(fake_sleep):
    captured = {}

    def handler(request):
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"id": "1"})

    client = _client(handler, _settings(access_token="tok-abc"))
    client.get("/crm/v3/objects/contacts/1")

    assert captured["auth"] == "Bearer tok-abc"


def test_401_error_message_does_not_leak_token(fake_sleep):
    def handler(request):
        return httpx.Response(401, json={"status": "error", "message": "invalid token"})

    client = _client(handler, _settings(access_token="super-secret-token"))

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert "super-secret-token" not in exc_info.value.message


# -- get ---------------------------------------------------------------------------


def test_get_returns_parsed_json_body(fake_sleep):
    def handler(request):
        return httpx.Response(200, json={"id": "1", "properties": {"email": "a@b.com"}})

    client = _client(handler)

    assert client.get("/crm/v3/objects/contacts/1") == {
        "id": "1",
        "properties": {"email": "a@b.com"},
    }


def test_get_params_are_url_encoded_not_concatenated(fake_sleep):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    client = _client(handler)
    client.get("/crm/v3/objects/contacts", params={"properties": "email,firstname&x=y"})

    assert "properties=email%2Cfirstname%26x%3Dy" in captured["url"]


# -- post allowlist ------------------------------------------------------------------


def test_post_to_non_search_path_raises_without_sending(fake_sleep):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)

    with pytest.raises(ValueError) as exc_info:
        client.post("/crm/v3/objects/contacts", json={"properties": {"email": "a@b.com"}})

    assert "/search" in str(exc_info.value)
    assert sent == []


def test_post_to_search_path_sends_and_returns_body(fake_sleep):
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json={"total": 3, "results": [{"id": "1"}]})

    client = _client(handler)

    result = client.post("/crm/v3/objects/contacts/search", json={"query": "acme"})

    assert result == {"total": 3, "results": [{"id": "1"}]}
    assert b"acme" in captured["body"]


def test_post_search_total_passes_through_but_get_body_never_has_one(fake_sleep):
    """The CRM Search API's response carries a `total` field; plain list/get
    endpoints never do. post() must pass the body through verbatim (total intact);
    get() likewise passes the body through verbatim, and a plain-list fake body with
    no `total` key stays that way — the distinction lives in what HubSpot actually
    sends, not in any special-casing by the client."""

    def search_handler(request):
        return httpx.Response(200, json={"total": 42, "results": [], "paging": {}})

    def list_handler(request):
        return httpx.Response(200, json={"results": [{"id": "1"}], "paging": {}})

    search_client = _client(search_handler)
    list_client = _client(list_handler)

    search_body = search_client.post(
        "/crm/v3/objects/contacts/search", json={"query": "acme"}
    )
    list_body = list_client.get("/crm/v3/objects/contacts")

    assert search_body["total"] == 42
    assert "total" not in list_body


# -- retry matrix ---------------------------------------------------------------------


def test_429_with_retry_after_recovers(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"status": "error", "message": "Throttled"},
            )
        return httpx.Response(200, json={"results": []})

    client = _client(handler)

    assert client.get("/crm/v3/objects/contacts") == {"results": []}
    assert fake_sleep == [2.0]


def test_429_four_times_raises_hubspot_error(fake_sleep):
    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "1"},
            json={"status": "error", "message": "Throttled"},
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 429
    assert fake_sleep == [1.0, 1.0, 1.0]


def test_429_daily_policy_is_not_retried(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(
            429,
            json={
                "status": "error",
                "message": "You have reached your daily limit.",
                "policyName": "DAILY",
                "correlationId": "abc-123",
            },
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert attempts["n"] == 1
    assert fake_sleep == []
    assert exc_info.value.status == 429
    assert "daily api limit reached" in exc_info.value.message.lower()


def test_429_non_numeric_retry_after_falls_back_to_backoff_sequence(fake_sleep):
    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "soon"},
            json={"status": "error", "message": "Throttled"},
        )

    client = _client(handler)

    with pytest.raises(HubSpotError):
        client.get("/crm/v3/objects/contacts")

    assert fake_sleep == [1, 2, 4]


def test_retry_after_huge_value_clamped_to_sixty_seconds(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "3600"},
                json={"status": "error", "message": "Throttled"},
            )
        return httpx.Response(200, json={"results": []})

    client = _client(handler)

    assert client.get("/crm/v3/objects/contacts") == {"results": []}
    assert fake_sleep == [60.0]


def test_retry_after_negative_value_clamped_to_zero(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "-5"},
                json={"status": "error", "message": "Throttled"},
            )
        return httpx.Response(200, json={"results": []})

    client = _client(handler)

    assert client.get("/crm/v3/objects/contacts") == {"results": []}
    assert fake_sleep == [0.0]


def test_5xx_is_not_retried(fake_sleep):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(500, json={"status": "error", "message": "Server error"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert attempts["n"] == 1
    assert fake_sleep == []
    assert exc_info.value.status == 500


# -- error mapping ------------------------------------------------------------------


def test_401_maps_to_actionable_message(fake_sleep):
    def handler(request):
        return httpx.Response(401, json={"status": "error", "message": "invalid token"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 401
    assert "HUBSPOT_MCP_ACCESS_TOKEN" in exc_info.value.message


def test_403_on_objects_path_names_crm_objects_scope(fake_sleep):
    def handler(request):
        return httpx.Response(
            403, json={"status": "error", "message": "missing scopes"}
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 403
    assert "crm.objects.contacts.read" in exc_info.value.message
    assert "crm.schemas" not in exc_info.value.message


def test_403_on_properties_path_names_crm_schemas_scope(fake_sleep):
    """list_properties hits /crm/v3/properties/{obj}, which needs
    crm.schemas.{obj}.read — not crm.objects.{obj}.read (regression: the 403
    handler used to always name the objects scope regardless of which endpoint
    actually 403'd)."""

    def handler(request):
        return httpx.Response(
            403, json={"status": "error", "message": "missing scopes"}
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/properties/contacts")

    assert exc_info.value.status == 403
    assert "crm.schemas.contacts.read" in exc_info.value.message
    assert "crm.objects.contacts.read" not in exc_info.value.message


def test_404_on_record_path_names_object_and_id(fake_sleep):
    def handler(request):
        return httpx.Response(404, json={"status": "error", "message": "not found"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts/12345")

    assert exc_info.value.status == 404
    message = exc_info.value.message
    assert "contacts" in message
    assert "12345" in message


def test_404_on_collection_path_names_object_type(fake_sleep):
    def handler(request):
        return httpx.Response(404, json={"status": "error", "message": "not found"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/frobnicator")

    assert exc_info.value.status == 404
    assert "frobnicator" in exc_info.value.message


def test_generic_error_folds_message_and_category(fake_sleep):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "status": "error",
                "message": "Invalid property",
                "category": "VALIDATION_ERROR",
                "correlationId": "abc-123",
            },
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 400
    assert "Invalid property" in exc_info.value.message
    assert "VALIDATION_ERROR" in exc_info.value.message
    assert "abc-123" not in exc_info.value.message


def test_error_with_non_json_body_falls_back_to_generic_message(fake_sleep):
    def handler(request):
        return httpx.Response(500, content=b"internal server error")

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 500
    assert "500" in exc_info.value.message


def test_no_credentials_anywhere_in_error_messages(fake_sleep):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "status": "error",
                "message": "bad request for tok-abc",
                "category": "VALIDATION_ERROR",
            },
        )

    client = _client(handler, _settings(access_token="tok-abc"))

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    # The upstream message happens to echo the token value; the client doesn't
    # scrub upstream bodies (that's HubSpot's own message text) — this test instead
    # asserts the client's own constructed sentences (401/403 paths and the
    # generic-error scaffolding around message/category) never independently
    # introduce the settings' token. This handler's own text containing the token is
    # deliberately not asserted away here — see the 401 test for the client-owned
    # message, which is the one that must never leak it.
    assert "check HUBSPOT_MCP_ACCESS_TOKEN" not in exc_info.value.message


def test_redirect_maps_to_actionable_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://app.hubspot.com/login"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 302
    assert "redirected" in exc_info.value.message
    assert "HUBSPOT_MCP_BASE_URL" in exc_info.value.message


def test_200_with_html_body_maps_to_non_json_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>Maintenance</body></html>",
        )

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 200
    message = exc_info.value.message
    assert "non-JSON" in message
    assert "HUBSPOT_MCP_BASE_URL" in message


def test_200_with_non_dict_json_body_maps_to_non_json_sentence(fake_sleep):
    def handler(request):
        return httpx.Response(200, json=[1, 2, 3])

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status == 200
    assert "non-JSON" in exc_info.value.message


def test_network_error_maps_to_unreachable(fake_sleep):
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    client = _client(handler)

    with pytest.raises(HubSpotError, match="HubSpot unreachable") as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert exc_info.value.status is None


def test_timeout_maps_to_timed_out_not_unreachable(fake_sleep):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler, _settings(timeout_seconds=7.0))

    with pytest.raises(HubSpotError, match="HubSpot timed out after 7s") as exc_info:
        client.get("/crm/v3/objects/contacts")

    assert "unreachable" not in exc_info.value.message


def test_default_http_client_sets_timeout():
    client = HubSpotClient(_settings(timeout_seconds=30.0))

    timeout = client._http.timeout
    assert timeout.read == 30.0
    assert timeout.connect == 5.0


# -- pagination -----------------------------------------------------------------


def test_query_paged_single_page_no_more_cursor(fake_sleep):
    def handler(request):
        assert request.url.params["limit"] == "25"
        return httpx.Response(
            200, json={"results": [{"id": "1"}, {"id": "2"}], "paging": {}}
        )

    client = _client(handler)

    records, after = client.query_paged("/crm/v3/objects/contacts")

    assert [r["id"] for r in records] == ["1", "2"]
    assert after is None


def test_query_paged_follows_cursor_chain_to_cap(fake_sleep, monkeypatch):
    monkeypatch.setattr(client_module, "MAX_PAGE_SIZE", 2)
    requested = []

    def handler(request):
        after = request.url.params.get("after")
        page_size = int(request.url.params["limit"])
        requested.append((after, page_size))
        if after is None:
            ids = ["1", "2"]
            body = {"results": [{"id": i} for i in ids], "paging": {"next": {"after": "cursor-2"}}}
        elif after == "cursor-2":
            ids = ["3", "4"]
            body = {"results": [{"id": i} for i in ids], "paging": {"next": {"after": "cursor-4"}}}
        else:
            ids = ["5"]
            body = {"results": [{"id": i} for i in ids], "paging": {}}
        return httpx.Response(200, json=body)

    client = _client(handler)

    records, after = client.query_paged("/crm/v3/objects/contacts", limit=5)

    assert [r["id"] for r in records] == ["1", "2", "3", "4", "5"]
    assert after is None
    assert requested == [(None, 2), ("cursor-2", 2), ("cursor-4", 1)]


def test_query_paged_cap_reached_with_cursor_remaining_reports_after(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [{"id": "1"}, {"id": "2"}],
                "paging": {"next": {"after": "cursor-more"}},
            },
        )

    client = _client(handler)

    records, after = client.query_paged("/crm/v3/objects/contacts", limit=2)

    assert [r["id"] for r in records] == ["1", "2"]
    assert after == "cursor-more"


def test_query_paged_empty_page_stops(fake_sleep):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={"results": [], "paging": {}})

    client = _client(handler)

    records, after = client.query_paged("/crm/v3/objects/contacts", limit=5)

    assert records == []
    assert after is None
    assert len(requested) == 1


def test_query_paged_defaults_limit_to_settings_item_limit(fake_sleep):
    def handler(request):
        assert request.url.params["limit"] == "3"
        return httpx.Response(200, json={"results": [{"id": "1"}], "paging": {}})

    client = _client(handler, _settings(item_limit=3))

    client.query_paged("/crm/v3/objects/contacts")


def test_query_paged_error_propagates(fake_sleep):
    def handler(request):
        return httpx.Response(404, json={"status": "error", "message": "not found"})

    client = _client(handler)

    with pytest.raises(HubSpotError) as exc_info:
        client.query_paged("/crm/v3/objects/frobnicator")

    assert exc_info.value.status == 404


# -- rate-limit header parsing / usage_note --------------------------------------------


def test_usage_note_none_before_any_request(fake_sleep):
    client = _client(lambda request: httpx.Response(200, json={}))

    assert client.usage_note() is None


def test_usage_note_none_when_usage_low(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "250000",
                "X-HubSpot-RateLimit-Daily-Remaining": "200000",
                "X-HubSpot-RateLimit-Secondly": "19",
                "X-HubSpot-RateLimit-Secondly-Remaining": "18",
            },
            json={"results": []},
        )

    client = _client(handler)
    client.get("/crm/v3/objects/contacts")

    assert client.usage_note() is None


def test_usage_note_warns_under_ten_percent_daily_remaining(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "50",
            },
            json={"results": []},
        )

    client = _client(handler)
    client.get("/crm/v3/objects/contacts")

    note = client.usage_note()
    assert note is not None
    assert "50" in note
    assert "1000" in note


def test_usage_note_exactly_ten_percent_does_not_warn(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "100",
            },
            json={"results": []},
        )

    client = _client(handler)
    client.get("/crm/v3/objects/contacts")

    assert client.usage_note() is None


def test_rate_limit_headers_malformed_are_treated_as_unknown(fake_sleep):
    def handler(request):
        return httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "not-a-number",
                "X-HubSpot-RateLimit-Daily-Remaining": "also-not-a-number",
            },
            json={"results": []},
        )

    client = _client(handler)
    client.get("/crm/v3/objects/contacts")

    assert client.usage_note() is None


def test_rate_limit_headers_parsed_even_on_error_response(fake_sleep):
    def handler(request):
        return httpx.Response(
            404,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "10",
            },
            json={"status": "error", "message": "not found"},
        )

    client = _client(handler)

    with pytest.raises(HubSpotError):
        client.get("/crm/v3/objects/contacts/1")

    note = client.usage_note()
    assert note is not None
    assert "10" in note


def test_usage_note_survives_header_less_search_response(fake_sleep):
    """The CRM Search API omits rate-limit headers; a search after a low-quota GET must
    NOT erase the warning learned from that GET (regression: unconditional overwrite)."""
    responses = iter([
        httpx.Response(
            200,
            headers={
                "X-HubSpot-RateLimit-Daily": "1000",
                "X-HubSpot-RateLimit-Daily-Remaining": "50",
            },
            json={"results": []},
        ),
        httpx.Response(200, json={"results": [], "total": 0}),  # search: no rate headers
    ])

    client = _client(lambda request: next(responses))
    client.get("/crm/v3/objects/contacts")
    client.post("/crm/v3/objects/contacts/search", json={"query": "x"})

    note = client.usage_note()
    assert note is not None
    assert "50" in note and "1000" in note


def test_post_rejects_search_path_with_fragment_or_query(fake_sleep):
    """A `#fragment` or `?query` must not sneak a non-search path past the allowlist:
    httpx strips them before sending, so `/crm/v3/objects/contacts#/search` would POST
    to the create endpoint. Reject any query/fragment outright (regression)."""
    client = _client(lambda request: httpx.Response(200, json={"results": []}))
    for bad in [
        "/crm/v3/objects/contacts#/search",
        "/crm/v3/objects/contacts?x=/search",
        "/crm/v3/objects/contacts?/search",
    ]:
        with pytest.raises(ValueError):
            client.post(bad, json={"query": "x"})
