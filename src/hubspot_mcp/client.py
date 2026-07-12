"""Thin httpx-based HubSpot CRM API client: request/retry/paginate, nothing else.
Read-only by construction — `get` and `post` are the only entry points anywhere in
this module, and `post` is itself hard-allowlisted to paths ending in `/search`: the
CRM Search API is the one read HubSpot models as a POST (the filter body is too large
for a query string). There is no `put`, `patch`, or `delete`; that asymmetry with a
typical API client is the whole safety story of this server, so it lives at the
client surface, not just in how the MCP tools happen to use it.

Every request goes through `_request`, which is the single place that: builds the
bearer auth header, parses HubSpot's rate-limit headers off every response (so
`usage_note()` can warn before a caller runs out of daily quota), retries 429s
honoring `Retry-After` (except a 429 whose body names the DAILY policy — no amount of
retrying fixes that), and reduces any failure to a `HubSpotError` whose `.message` is
an actionable, credential-free sentence safe to return verbatim as an MCP tool result
— never a raw HubSpot error body or traceback, and never the access token used to
make the request.

The daily rate-limit header names (`X-HubSpot-RateLimit-Daily[-Remaining]` — the only
rate-limit headers this client reads; the legacy per-second "Secondly" burst headers
are not consumed), the 429 error body's `policyName` field, the `{status, message,
category, correlationId}` error envelope, and the `paging.next.after` cursor shape
are verified against HubSpot's developer docs:
https://developers.hubspot.com/docs/api/usage-details (rate-limit headers, 429 body)
https://developers.hubspot.com/docs/api/crm/search (search request/response shape,
`total`, `paging.next.after`)
https://developers.hubspot.com/docs/api/error-handling ({status, message, category,
correlationId} error envelope)
https://developers.hubspot.com/docs/guides/apps/private-apps/overview (Bearer token
auth for Private Apps)
"""

from time import sleep

import httpx
from urllib.parse import urlsplit

from hubspot_mcp.settings import Settings

# Retries after the initial attempt: 3 more tries (4 requests total) before giving up.
# Only 429s are retried — HubSpot 5xx responses get a single actionable sentence and
# no retry, since a retry loop hiding a real outage is worse than a fast, honest
# failure.
MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 2, 4)

# Per-request cap on HubSpot's `limit` param, independent of Settings.item_limit
# (which is the MCP tools' default *result* size, a UX choice). query_paged never
# asks HubSpot for more than this many records in a single request, no matter how
# large a `limit` a caller asks for.
MAX_PAGE_SIZE = 100


class HubSpotError(Exception):
    """Raised for any HubSpot request that fails after the client's retry policy is
    exhausted. `.message` is an actionable, credential-free sentence; `.status` is
    the terminal HTTP status code, or None for a network-level failure (no response
    was ever received — DNS, connection refused, timeout)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_retry_after(value: str | None) -> float | None:
    """HubSpot's 429 Retry-After is a delta-seconds integer in practice, not an
    HTTP-date. Fall back to the backoff sequence for anything we can't parse."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_int_header(headers: httpx.Headers, name: str) -> int | None:
    """Best-effort int from a rate-limit header. A missing or non-integer value (a
    hostile or buggy upstream) must never crash rate-limit tracking — it's just
    treated as unknown for that response."""
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _hubspot_error_detail(
    response: httpx.Response,
) -> tuple[str | None, str | None, str | None]:
    """Best-effort (message, category, policyName) from a HubSpot JSON error body:
    `{"status": "error", "message": ..., "category": ..., "correlationId": ...}`,
    with `policyName` (e.g. "DAILY") present on 429 bodies. Never raises — a
    non-JSON or unexpected body just yields (None, None, None) and the caller falls
    back to a generic status-code message."""
    try:
        body = response.json()
    except ValueError:
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None
    return body.get("message"), body.get("category"), body.get("policyName")


def _parse_object_path(path: str) -> tuple[str | None, str | None, str | None]:
    """Best-effort (anchor, object_type, record_id) for an actionable 403/404 message,
    e.g. '/crm/v3/objects/contacts/12345' -> ('objects', 'contacts', '12345') and
    '/crm/v3/objects/contacts' -> ('objects', 'contacts', None). Also recognizes the
    `/crm/v3/properties/{obj}` schema-discovery shape, e.g.
    '/crm/v3/properties/contacts' -> ('properties', 'contacts', None) — `anchor` is
    what lets `_error_for` tell a data-scope 403 (crm.objects.*.read) apart from a
    schema-scope one (crm.schemas.*.read). Falls back to (None, None, None) for any
    path that doesn't follow either shape."""
    parts = [segment for segment in path.split("/") if segment]
    for anchor in ("objects", "properties"):
        if anchor in parts:
            index = parts.index(anchor)
            obj = parts[index + 1] if index + 1 < len(parts) else None
            following = parts[index + 2] if index + 2 < len(parts) else None
            # "search" is a path suffix, not a record id (post() only ever reaches
            # this helper via a 4xx on a /search POST).
            record_id = following if following and following != "search" else None
            return anchor, obj, record_id
    return None, None, None


def _parse_json_body(response: httpx.Response) -> dict:
    """Parse a response that already passed the status-code gate in `_request` as a
    JSON object, raising an actionable HubSpotError if it isn't. A wrong base URL or
    a HubSpot maintenance page in front of the API commonly answers a 200 with an
    HTML page instead of the usual JSON envelope — this is the one place `get` and
    `post` funnel through so that failure mode gets the same actionable message
    everywhere rather than a raw JSONDecodeError or AttributeError escaping to a tool
    caller."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HubSpotError(
            response.status_code,
            f"HubSpot returned a non-JSON response (HTTP {response.status_code}) — "
            "check HUBSPOT_MCP_BASE_URL; a wrong base URL or a HubSpot maintenance "
            "page commonly causes this.",
        )
    return body


def _next_after(body: dict) -> str | None:
    """Best-effort `paging.next.after` cursor from a parsed response body, per
    https://developers.hubspot.com/docs/api/crm/search. Absent (None) means there is
    no further page."""
    paging = body.get("paging")
    next_page = paging.get("next") if isinstance(paging, dict) else None
    return next_page.get("after") if isinstance(next_page, dict) else None


class HubSpotClient:
    """Bearer-token HubSpot CRM API client, per `Settings`."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self._settings = settings
        self._base_url = settings.base_url
        self._timeout_seconds = settings.timeout_seconds
        self._item_limit = settings.item_limit
        self._http = (
            http
            if http is not None
            else httpx.Client(
                timeout=httpx.Timeout(settings.timeout_seconds, connect=5.0)
            )
        )
        self._daily: int | None = None
        self._daily_remaining: int | None = None

    def get(self, path: str, params: dict | None = None) -> dict:
        return _parse_json_body(self._request("GET", path, params=params))

    def post(self, path: str, json: dict | None = None) -> dict:
        """POST is allowlisted to the CRM Search API only — the one read HubSpot
        models as a POST. Any other path is rejected with a ValueError *before* any
        request is sent; there is no way to reach a non-search write endpoint through
        this client."""
        # Parse the path and check the PATH httpx will actually request, not the raw
        # string: httpx strips a `#fragment` / `?query` before sending, so a raw string
        # like "/crm/v3/objects/contacts#/search" ends in "/search" yet resolves on the
        # wire to the (write) create endpoint. Reject any query or fragment outright — a
        # search POST never has one — and require the real path to end in "/search".
        parsed = urlsplit(path)
        if parsed.query or parsed.fragment or not parsed.path.endswith("/search"):
            raise ValueError(
                "HubSpotClient.post() only sends requests to the CRM Search API "
                f"(paths ending in '/search', no query or fragment) — refusing to POST "
                f"to {path!r}. This client is read-only by construction: post() is the "
                "only write-verb method it exposes, and it is allowlisted to search "
                "paths; there is no put/patch/delete method at all."
            )
        return _parse_json_body(self._request("POST", path, json=json))

    def query_paged(
        self, path: str, params: dict | None = None, limit: int | None = None
    ) -> tuple[list[dict], str | None]:
        """Fetch up to `limit` records (defaulting to Settings.item_limit) from a
        GET list endpoint, following HubSpot's `paging.next.after` cursor in chunks
        of at most MAX_PAGE_SIZE — never one giant request, and never more requests
        than needed to satisfy `limit`. Returns (records, after) where `after` is the
        cursor for the next page if one remains (whether because the cap was reached
        or HubSpot simply has more), or None once the result set is exhausted. Plain
        list endpoints never report a `total`, so this leftover cursor is the
        equivalent "more available" signal callers have to work with — contrast the
        CRM Search API, whose response body carries an actual `total` and is read
        directly via `post()` rather than through this helper."""
        cap = limit if limit is not None else self._item_limit
        base_params = dict(params or {})
        records: list[dict] = []
        after: str | None = None
        while len(records) < cap:
            page_size = min(cap - len(records), MAX_PAGE_SIZE)
            request_params = {**base_params, "limit": page_size}
            if after is not None:
                request_params["after"] = after
            body = self.get(path, params=request_params)
            page = body.get("results", [])
            if isinstance(page, list):
                records.extend(page)
            after = _next_after(body)
            if not page or after is None:
                break
        return records[:cap], after

    def usage_note(self) -> str | None:
        """An actionable warning once daily quota usage crosses 90%, or None when
        usage is fine or no rate-limit headers have been seen yet (e.g. before the
        first request, or against a test double that doesn't send them)."""
        if (
            self._daily is None
            or self._daily_remaining is None
            or self._daily <= 0
        ):
            return None
        if self._daily_remaining < 0.10 * self._daily:
            return (
                f"HubSpot daily API limit usage is high: {self._daily_remaining} of "
                f"{self._daily} requests remaining today."
            )
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.access_token}"}

    def _resolve_url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _update_rate_limit(self, response: httpx.Response) -> None:
        # Parsed on every response, success or error, so usage_note() reflects the
        # most recent quota state even when the response itself ends up erroring or
        # being retried. A field is updated ONLY when its header is present: the CRM
        # Search API omits rate-limit headers entirely, so overwriting unconditionally
        # would erase a real quota warning learned from a prior GET on every search call.
        for attr, header in (
            ("_daily", "X-HubSpot-RateLimit-Daily"),
            ("_daily_remaining", "X-HubSpot-RateLimit-Daily-Remaining"),
        ):
            value = _parse_int_header(response.headers, header)
            if value is not None:
                setattr(self, attr, value)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        url = self._resolve_url(path)
        headers = {**self._auth_headers(), "Accept": "application/json"}
        retries = 0
        while True:
            try:
                response = self._http.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.TimeoutException as exc:
                # Distinct from "unreachable" — HubSpot was reachable but too slow,
                # so the message doesn't send someone chasing a network/DNS problem
                # that isn't there.
                raise HubSpotError(
                    None,
                    f"HubSpot timed out after {self._timeout_seconds:.0f}s: {exc}",
                ) from exc
            except httpx.HTTPError as exc:
                # Never let the underlying exception (which may echo the request,
                # headers and all — including Authorization) reach the caller —
                # reduce it to a plain, credential-free sentence.
                raise HubSpotError(None, f"HubSpot unreachable: {exc}") from exc

            self._update_rate_limit(response)

            if response.status_code == 429:
                _, _, policy_name = _hubspot_error_detail(response)
                if policy_name == "DAILY":
                    # No amount of retrying recovers from this within the retry
                    # window — the daily quota only resets at midnight portal time.
                    raise HubSpotError(
                        429,
                        "HubSpot daily API limit reached — it resets at midnight "
                        "portal time; wait or raise the Private App's limit before "
                        "retrying.",
                    )
                if retries < MAX_RETRIES:
                    delay = _parse_retry_after(response.headers.get("Retry-After"))
                    if delay is None:
                        delay = BACKOFF_SECONDS[retries]
                    # HubSpot is trusted to send sane values, but a hostile or buggy
                    # upstream sending an absurd (hours-long) or negative Retry-After
                    # must not be able to hang the process or sleep(-N)-crash it.
                    delay = max(0.0, min(delay, 60.0))
                    retries += 1
                    sleep(delay)
                    continue
                raise HubSpotError(
                    429,
                    f"HubSpot is still rate-limiting requests after {MAX_RETRIES} "
                    "retries (HTTP 429) — try again shortly.",
                )

            if 300 <= response.status_code < 400:
                # follow_redirects is off (httpx's default) by design — a redirect
                # means something other than the CRM API answered, most commonly a
                # wrong base URL or a portal-level maintenance/login page. Neither is
                # safe to silently follow with a bearer token attached, so surface it
                # instead of chasing it.
                raise HubSpotError(
                    response.status_code,
                    "HubSpot redirected the request — check HUBSPOT_MCP_BASE_URL; "
                    "this usually means the wrong base URL or a portal in "
                    "maintenance.",
                )

            if response.status_code >= 400:
                raise self._error_for(response, path)

            return response

    def _error_for(self, response: httpx.Response, path: str) -> HubSpotError:
        status = response.status_code
        if status == 401:
            return HubSpotError(
                401,
                "HubSpot rejected the token — check HUBSPOT_MCP_ACCESS_TOKEN and "
                "its scopes.",
            )
        if status == 403:
            anchor, obj, _ = _parse_object_path(path)
            obj_part = obj or "this object"
            # /crm/v3/properties/{obj} (schema discovery, list_properties) needs
            # crm.schemas.{obj}.read; every /crm/v3|v4/objects/{obj}... path (record
            # reads/search/associations) needs crm.objects.{obj}.read instead.
            scope_kind = "schemas" if anchor == "properties" else "objects"
            return HubSpotError(
                403,
                "The token lacks the scope for this object — add the "
                f"crm.{scope_kind}.{obj_part}.read scope to the Private App.",
            )
        if status == 404:
            _, obj, record_id = _parse_object_path(path)
            if obj and record_id:
                return HubSpotError(404, f"No {obj} found with id {record_id}.")
            if obj:
                return HubSpotError(
                    404, f"No {obj} object type found — check the object_type name."
                )
            return HubSpotError(404, "HubSpot returned HTTP 404 for this request.")
        message, category, _ = _hubspot_error_detail(response)
        parts = [text for text in (message, category) if text]
        suffix = f": {'; '.join(parts)}" if parts else ""
        return HubSpotError(status, f"HubSpot returned HTTP {status}{suffix}.")
