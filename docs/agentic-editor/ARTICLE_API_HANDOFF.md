# Handoff to the consuming application

**Deployment prerequisite:** this handoff describes the hardened API contract.
Deploy the hardening migration/code and provision a scoped reader token before
using it. The earlier title-search release required a caller-supplied `user_id`;
that release alone does not provide the security described here.

Give the next application's developer or coding agent these two files:

- `ARTICLE_SEARCH_API.md`: full human-readable API and operator guide.
- `ARTICLE_SEARCH_API.openapi.json`: machine-readable OpenAPI 3.1 contract,
  suitable for importing into API tooling or generating a client.

Copy these files into the next app's integration documentation, attach them to
its development chat, or share their GitHub links after this change is published.
The docs are safe to share within your organization; they contain no real token.

Supply the credential separately through that app's backend secret manager:

| Setting | Value |
| --- | --- |
| `EDITOR_BASE_URL` | `https://editor.agentictopic.com` |
| `EDITOR_API_TOKEN` | Dedicated operator-provisioned `articles_read` token |

Do not include the token in a prompt, documentation, URL, browser bundle, or Git.
Send the token only to the configured editor origin; never send it to a CMS edit
link, article source, or another host. The next app does not need your CMS login
or an actor/user ID. Its token fixes its application identity and accessible regions on the server. Rotation replaces the
old token immediately; coordinate the secret update with the operator before
its expiry (90 days by default).

## Copyable integration brief

> Integrate the Agentic AI Editor article API on our backend. Read the supplied
> API guide and OpenAPI specification. Use `EDITOR_BASE_URL` and the backend
> secret `EDITOR_API_TOKEN`, sending `Authorization: Bearer <token>` on every
> request. Search with `GET /api/articles/search?q=<URL-encoded title substring>`;
> iterate through `pagination.total_pages` if needed. Each result contains the
> full reviewed article fields. Use its local `id` for `GET /api/articles/:id`
> when a refreshed record is needed. Use `cms_edit_url` for the external CMS edit
> link and `cms_article_reference` for the remote edit ID or slug. Local IDs and
> CMS references are different. CMS links may be null for local-only records.
> Omit `user_id`. Treat 401 as invalid/expired/revoked credentials, 403 as denied
> access, 404 as unavailable/out-of-scope content, 429 as throttling, and 503 as
> temporary unavailability. Honor `Retry-After` when present and use bounded
> backoff/timeouts. Do not expose the token to browsers or try editorial writes.

Example request from the consuming backend:

```sh
curl --fail-with-body --get "$EDITOR_BASE_URL/api/articles/search" \
  --header "Authorization: Bearer $EDITOR_API_TOKEN" \
  --header 'Accept: application/json' \
  --data-urlencode 'q=river reopens' \
  --data-urlencode 'limit=50'
```

Before connecting real traffic, verify a known in-scope title returns the
expected local ID and CMS link, an out-of-scope record returns 404, and a write
attempt using the reader token returns 403. The operator must choose the approved application scope or user/regions and
provision the credential; the API docs alone are not credentials.
