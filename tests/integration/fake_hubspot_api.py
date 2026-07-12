"""In-process fake HubSpot CRM API for the stdio round-trip gate
(tests/integration/test_stdio_roundtrip.py).

Runs as a background *thread* inside the pytest process — never a separate
process — on an OS-assigned port (bind port 0), so the gate has no fixed-port
collision risk and needs no process cleanup beyond `stop()`.

Serves just enough of the CRM API shapes the five real MCP tools hit
(list/get/search on `crm/v3/objects/contacts`, `crm/v4/.../associations`,
`crm/v3/properties/contacts`) to drive list_records, get_record,
search_records, get_associations, and list_properties end-to-end, matching
the response envelopes HubSpotClient/server.py were verified against (see
their module docstrings): `results` + `paging.next.after` on list endpoints,
`total` + `results` on search, and `results[].toObjectId` +
`results[].associationTypes[].{category,typeId,label}` on v4 associations.

Every request received (method + path, ignoring query string) is recorded so
the gate can assert, after driving all five tools over stdio, that the only
POST ever reached was the one allowlisted `/search` call — proof that the
read-only guard holds end-to-end, not just at the client's unit-test
boundary.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence request logging in test output
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method):
        path = urlsplit(self.path).path
        self.server.record_request(method, path)
        return path

    def do_GET(self):
        path = self._record("GET")

        if path == "/crm/v3/objects/contacts":
            self._json(
                {
                    "results": [
                        {
                            "id": "101",
                            "properties": {
                                "email": "ann@example.com",
                                "firstname": "Ann",
                                "lastname": "Lee",
                            },
                        }
                    ],
                    "paging": {},
                }
            )
        elif path == "/crm/v3/objects/contacts/101":
            self._json(
                {
                    "id": "101",
                    "properties": {
                        "email": "ann@example.com",
                        "firstname": "Ann",
                        "lastname": "Lee",
                    },
                }
            )
        elif path == "/crm/v4/objects/companies/500/associations/contacts":
            self._json(
                {
                    "results": [
                        {
                            "toObjectId": 101,
                            "associationTypes": [
                                {
                                    "category": "HUBSPOT_DEFINED",
                                    "typeId": 279,
                                    "label": None,
                                }
                            ],
                        }
                    ]
                }
            )
        elif path == "/crm/v3/properties/contacts":
            self._json(
                {
                    "results": [
                        {"name": "email", "type": "string", "label": "Email"},
                        {"name": "firstname", "type": "string", "label": "First Name"},
                        {"name": "lastname", "type": "string", "label": "Last Name"},
                    ]
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self._record("POST")

        if path == "/crm/v3/objects/contacts/search":
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)  # drain the body; content isn't asserted here
            self._json(
                {
                    "total": 1,
                    "results": [
                        {
                            "id": "101",
                            "properties": {
                                "email": "ann@example.com",
                                "firstname": "Ann",
                                "lastname": "Lee",
                            },
                        }
                    ],
                }
            )
        else:
            # Any non-/search POST is a would-be write path — HubSpotClient.post()
            # should never send one, so this branch is only exercised if that
            # guard has already failed. Answer 501 (not 200) so a captured-request
            # assertion has an obviously-wrong response to point at, too.
            self.send_response(501)
            self.end_headers()


class FakeHubSpotAPI:
    """Thread-backed fake CRM API bound to 127.0.0.1 on an OS-assigned port."""

    def __init__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.record_request = self._record_request
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._lock = threading.Lock()
        self._requests: list[tuple[str, str]] = []

    def _record_request(self, method: str, path: str) -> None:
        with self._lock:
            self._requests.append((method, path))

    @property
    def requests(self) -> list[tuple[str, str]]:
        """A snapshot of every (method, path) request received so far, in order."""
        with self._lock:
            return list(self._requests)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeHubSpotAPI":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()
