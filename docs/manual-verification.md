# Manual verification (live HubSpot portal)

CI runs the full suite against a faked CRM API and never touches a real HubSpot
portal. Before a release, a maintainer runs this script once against a real HubSpot
portal to verify the end-to-end path CI cannot: real Private App auth, real CRM API
responses, real rate-limit headers.

Prerequisites: a HubSpot portal (a free developer account and test portal are
available at [developers.hubspot.com](https://developers.hubspot.com/) — a few
minutes to provision) and a Private App access token created per the README's setup
walkthrough, scoped to at least `crm.objects.contacts.read` and
`crm.schemas.contacts.read`. A test portal that already has some sample contacts
(HubSpot seeds a few by default, or add one or two by hand) makes steps 2–5 easy to
eyeball.

1. Set the environment and start the server from this checkout:

       export HUBSPOT_MCP_ACCESS_TOKEN=<your-private-app-token>
       uv run hubspot-mcp-server

   Then connect an MCP client to it — easiest is `claude mcp add hubspot-dev -e
   HUBSPOT_MCP_ACCESS_TOKEN=$HUBSPOT_MCP_ACCESS_TOKEN -- uv run --directory
   <this-checkout> hubspot-mcp-server` and a fresh `claude` session.

2. Call `list_records` with `object_type=contacts`. **Expected:** a list of contact
   blocks, each starting with `id:`, followed by `email:`/`firstname:`/`lastname:`
   (the default contacts properties) for whichever fields are populated. Note one
   `id` from the output for the next step.

3. Call `get_record` with `object_type=contacts` and the `id` from step 2.
   **Expected:** the same contact's field set, confirming ids from a list call
   compose directly into a follow-up lookup with no client-side editing.

4. Call `search_records` with `object_type=contacts` and a plain-text `query` likely
   to match (e.g. part of a contact's name or email domain from step 2/3).
   **Expected:** hits with `id:` and the default properties, plus a "(showing N of
   total matches)" line when more matches exist than were returned — confirming the
   search endpoint's `total` field is read correctly, not just the unit-test
   fixtures.

5. Call `get_associations` with `object_type=contacts`, the `id` from step 2, and
   `to_object_type=companies` (or pick a contact/company pair you know is
   associated). **Expected:** either a `toObjectId:`/`associationTypes:` block per
   related company, or a clean "No companies associations found for..." message if
   the contact has none — either is a pass; a traceback is not.

6. Call `list_properties` with `object_type=contacts`. **Expected:** a list of
   `name (type): label` lines covering HubSpot's standard contact properties (e.g.
   `email`, `firstname`, `lastname`). Confirms schema discovery against a real
   `crm/v3/properties/contacts` response.

7. Sign-off note on bad auth: stop the server, set `HUBSPOT_MCP_ACCESS_TOKEN` to
   something wrong, and restart. **Expected:** the first tool call returns "HubSpot
   rejected the token — check HUBSPOT_MCP_ACCESS_TOKEN and its scopes." with no
   token value echoed anywhere in the response. Restore the correct token
   afterward.

8. Sign-off note on rate-limit awareness: the 90%-daily-usage warning and the 429
   retry/backoff path can't be forced on demand against a live portal without
   burning real quota, so both are verified by the unit suite
   (`tests/test_client.py`) rather than this script. If the daily-usage warning
   happens to appear on any tool result during steps 2–6 (a portal already near its
   daily cap), that's a bonus real-world confirmation, not something to chase.

Record the date, portal name, and outcome of a run in the release PR description.
