# Article title search and retrieval API

This is the **Agentic AI Editor application's API**. Use it to search locally
stored article titles, read the complete stored article record, and obtain the
external Hoodline CMS edit link and reference. It does not search the remote CMS
live. Imported articles reflect the latest data stored in this application.

The two read endpoints are:

| Method | Path | Response |
| --- | --- | --- |
| GET | `/api/articles/search` | Matching full article records and pagination metadata |
| GET | `/api/articles/:id` | One full article record, including the same link fields |

## Connection and authentication

Use your deployment's HTTPS origin as `EDITOR_BASE_URL`, without a trailing slash.
For local development the origin is `http://localhost:3000`. All examples use
placeholder credentials and IDs; replace them with values supplied by your operator.

Each request needs `Authorization: Bearer <API_TOKEN>`. The operator binds that
client to an explicitly scoped application identity or an approved local user;
the caller cannot select its own actor.
**Do not send `user_id` in new integrations.** For compatibility, a supplied
`user_id` is accepted only for user-bound clients when it exactly matches that
user; application credentials reject every supplied `user_id`. Otherwise
HTTP 403 is returned.

`Accept: application/json` is recommended. GET requests have no request body.
`X-Api-Key: <API_TOKEN>` is supported as an alternative to Authorization. Send
exactly one credential header: supplying both is rejected. Bare tokens in the
Authorization header and query-string tokens are rejected. Browser session
cookies never grant API access. Tokens are 32–72 ASCII letters, digits, hyphens,
or underscores; generated tokens use 64 hexadecimal characters with 256 bits
of randomness. Use HTTPS in production and keep the token on the caller's backend.

### Operator provisioning, rotation, and revocation

For local development, use `docker compose exec web bundle exec rails console`.
In production, use the runbook's role-filtered Kamal console/runner from the
**hardened target image** after the migration; do not seed production to configure
an integration. Select the application scope or approved user explicitly. Application readers
do not need a human admin account. The following Ruby creates a reader credential:

```ruby
# Dedicated application reader: explicitly grant the intended scope.
client = ApiClient.new(name: "hl-corrections", access_mode: "articles_read", all_regions: true)
token = client.provision_token!
# Or restrict a reader to specific metros:
# client = ApiClient.new(name: "regional-reader", article_metros: ["Austin, TX"])
# For an approved user-bound client, pass user: approved_user instead.
# Transfer the token directly into the next app's secret store, then discard it.
# Do not paste it into chat, Git, deployment logs, or a handoff document.
```

`articles_read` is the default access mode. It allows GET/HEAD on the reviewed article read actions and
published-article feed; it denies all writes and all user/scheduling
endpoints, including when bound to an admin. An operator must explicitly assign
`access_mode: "editorial"` to enable the existing editorial API. Editorial clients require an approved bound user and remain subject to that
user's role. Application credentials cannot become writers without such a binding.

Tokens expire 90 days after provisioning by default. You may specify a reviewed
expiry with `client.provision_token!(expires_at: 30.days.from_now)`. Schedule
rotation in the consuming application's operations process before that date.
Rotating with `provision_token!` immediately invalidates the previous token and
clears the client's revocation flag. `client.revoke!` immediately disables it;
destroying the client also revokes it. Archiving or locking a bound user blocks access.
Expired/revoked/disabled or legacy-unconfigured tokens receive the same HTTP 401 response.
Only a bcrypt hash and an indexed SHA-256 fingerprint are stored, not the raw
secret; tokens cannot be retrieved later. `last_used_at` updates at most once
per five minutes, as an approximate usage signal.

### Upgrade existing credentials

The security migration adds user binding, access mode, expiry, revocation, and
fingerprint columns. It deliberately does not guess an owner or backfill broad
permissions. **Every pre-hardening client stops authenticating until an operator
assigns its application scope or approved user and rotates its token.** Coordinate all existing consumers before rollout.

```ruby
client = ApiClient.find_by!(name: "existing-consumer")
client.assign_attributes(user: nil, access_mode: "articles_read", all_regions: true)
# Only grant all regions if appropriate for that consumer. Alternatively, set
# all_regions: false, article_metros: approved_metros, or user: approved_user.
new_token = client.provision_token!
# Securely update that consumer's token; use editorial mode only if authorized.
```

Migration is additive and old app code can still run during the migration.
Complete credential preparation against the target image before web rollout.
Activate application credentials only once the hardened web is serving. For a
user-bound consumer during a mixed-version transition, a matching `user_id` may
be sent for compatibility with the old web; remove it after rollout. Tokens rejected by the hardened app must not be made
valid by bypassing scope/expiry checks. Production seeding requires an explicit
`PUBLISHED_ARTICLES_API_USER_ID` when provisioning the primary consumer.

### Authorization scope

An application reader can read all non-archived articles only when explicitly
configured with `all_regions: true`. Otherwise it can read only its
`article_metros`; an empty list grants no article access. A supplied `user_id`
cannot turn an application credential into a human actor.

For user-bound clients, an admin actor can read all non-archived articles. A non-admin actor can read
only metros in its `scope_regions`; **an empty region list grants no article
access**. These rules cover search, detail, lists, and the published feed, and
published-feed cache entries are separated by client, application scope, and
bound actor/role/region scope.
Archived records are excluded for every actor. Ignored records and all workflow
statuses remain searchable. Out-of-scope and absent IDs both return 404. API
responses send `Cache-Control: no-store`; do not cache private responses in a
shared/public cache.

## Search request

```sh
export EDITOR_BASE_URL='https://your-editor.example'
# Set EDITOR_API_TOKEN securely in your runtime environment.

curl --fail-with-body --get "$EDITOR_BASE_URL/api/articles/search" \
  --header "Authorization: Bearer $EDITOR_API_TOKEN" \
  --header 'Accept: application/json' \
  --data-urlencode 'q=river reopens' \
  --data-urlencode 'page=1' \
  --data-urlencode 'limit=50'
```

| Query parameter | Required | Rules |
| --- | --- | --- |
| `user_id` | No | Deprecated compatibility parameter; must match the token-bound actor if supplied. Omit it. |
| `q` | Yes | String; trim surrounding whitespace; 1–300 characters after trimming |
| `page` | No | Decimal integer 1–999999999; default 1 |
| `limit` | No | Decimal integer 1–200; default 50 |

Search uses PostgreSQL's case-insensitive substring match on **either `hl_title`
or `title`**. `hl_title` is the editorial headline; `title` is the original/imported
headline. A match in either field returns the article once. Body, excerpt, meta,
and other fields are not searched. The whole query is a contiguous substring,
not a list of words, a regular expression, or fuzzy/semantic search. `%`, `_`,
backslashes, and quotes are literal search characters, not SQL operators.
Case and accent behavior follows the database locale; accents are not stripped.
Use URL encoding (as in `--data-urlencode`) for spaces and punctuation.

Results are ordered by **local article ID ascending**. `total` counts all
matching visible records before pagination; `total_pages = ceil(total / limit)`.
Each page contains at most `limit` records. A search with no matches returns
HTTP 200 with `articles: []`, `total: 0`, and `total_pages: 0`. A page beyond the
last page also returns HTTP 200 and an empty array, retaining the actual totals.

This endpoint always paginates. Older article lists also default to bounded
pagination for reader tokens; editorial tokens retain their optional pagination. There is no response cache
for search or detail. Offset pages reflect current database state; changes between
requests may shift results. Narrow your query when many records match: search
uses substring matching and counts matches, and full records can be large.

## Search response

```json
{
  "articles": [
    {
      "id": 42,
      "hl_title": "Austin River Reopens",
      "title": "Original River Headline",
      "hl_meta": "Local news",
      "hl_excerpt": "The river has reopened.",
      "full_content": "<p>The river has reopened.</p>",
      "main_content": "The river has reopened.",
      "status": "requires_editing",
      "metro": "Austin, TX",
      "url": "hoodline-cms://articles/7654",
      "archived": false,
      "published_at": null,
      "cms_article_reference": "7654",
      "cms_edit_url": "https://hoodline.impress3.com/articles/7654/edit",
      "editor_edit_url": "https://your-editor.example/articles/42/edit",
      "api_url": "https://your-editor.example/api/articles/42"
    }
  ],
  "pagination": { "page": 1, "limit": 50, "total": 1, "total_pages": 1 },
  "query": "river reopens"
}
```

The article example is abbreviated for readability. Actual search results and
single-record responses include **the explicitly reviewed Article fields**, listed below,
and four additional link/reference fields. Unset values are JSON `null` (or the
column's stored empty object/array). Integers, booleans, and JSON columns keep
their JSON types. Dates and times are serialized as ISO 8601 strings. Except for `addresses` (deserialized by Rails from YAML into an array), text columns
remain strings even when their contents encode HTML or JSON; parse them separately
only when appropriate. Do not render returned HTML without your application's
normal sanitization. Related User records, their credentials, and CMS integration
credentials are not included. New database columns are not exposed automatically.
Clients should still tolerate explicitly added fields in later API revisions.

## CMS identity and links

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | integer | **Local Article ID**. Use this for `/api/articles/:id`. |
| `cms_article_reference` | string or null | Reference extracted from stored `url` with the form `hoodline-cms://articles/<reference>`. Usually a numeric ID, sometimes a slug; always returned as a string. This is the edit reference embedded between `/articles/` and `/edit` in the CMS URL. |
| `cms_edit_url` | string or null | Absolute external CMS edit link, using that reference. Its origin is `CMS_WEB_BASE_URL`, default `https://hoodline.impress3.com`. |
| `editor_edit_url` | string | Absolute edit link in this application, using local `id`. |
| `api_url` | string | Absolute single-record endpoint in this application, using local `id`. Supply the same credential header when following it; no actor query parameter is needed. |

For local-only articles, `cms_article_reference` and `cms_edit_url` are both
`null`. A normal public HTTP article URL is not treated as a CMS identity. Do
not infer a numeric CMS ID from a slug, or use local `id` as the CMS ID. These
links describe stored mappings; fetching a record does not verify that the
remote CMS record still exists. Opening either editor UI requires that UI's
own login and permissions. This API token does not log a browser into the CMS.

`CMS_WEB_BASE_URL` configures the CMS browser origin independently of
`CMS_API_BASE_URL` (the backend import/publish API origin). Set the former in the
application runtime and restart the application if your CMS UI lives elsewhere.
Local edit/API links use the request's host and scheme; call the canonical HTTPS
origin behind a correctly configured proxy in production.

## Fetch a selected record

Use `id` from a search result or its `api_url`:

```sh
curl --fail-with-body --get "$EDITOR_BASE_URL/api/articles/42" \
  --header "Authorization: Bearer $EDITOR_API_TOKEN" \
  --header 'Accept: application/json'
```

The HTTP 200 response is the article object directly, **without** an `articles`
wrapper or pagination. It has the same fields as each object in search results.
You can use the complete search record immediately or fetch again for its latest
stored state. The existing detail endpoint now adds the four link fields; its
persisted column names and values are preserved. Article list/write responses use the same reviewed article fields and additional
link fields. User API responses now omit credentials/security fields. Reader-token
article lists default to 50 records and cap at 200; editorial-token lists retain
optional pagination. Search always enforces the pagination rules above.

## Python integration example

This uses Python's standard library. It searches, iterates through every page,
and fetches each chosen article by local ID. In real applications, fetch detail
only if you need a newer record; search already includes the full fields.

```python
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base_url = os.environ["EDITOR_BASE_URL"].rstrip("/")
token = os.environ["EDITOR_API_TOKEN"]

def get_json(path, **params):
    request = Request(
        f"{base_url}{path}?{urlencode(params)}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)

page = 1
while True:
    result = get_json("/api/articles/search", q="river reopens", page=page, limit=50)
    for match in result["articles"]:
        article = get_json(f'/api/articles/{match["id"]}')
        print(article["hl_title"], article["cms_article_reference"], article["cms_edit_url"])
    if page >= result["pagination"]["total_pages"]:
        break
    page += 1
```

`urlopen` raises `HTTPError` for error statuses. Handle these in the calling
application according to the table below rather than silently treating them as
an empty search.

## Errors and limits

| Status | JSON body | Action |
| --- | --- | --- |
| 400 | `{"error":"q must be a nonblank string of at most 300 characters"}` | Supply a valid `q`; arrays/objects are rejected. |
| 400 | `{"error":"page must be a positive integer; limit must be an integer from 1 to 200"}` | Fix pagination. Zero, negatives, decimals, leading zeros, blanks, arrays, and non-numbers are rejected. `page` must have at most nine digits. |
| 401 | `{"error":"Unauthorized"}` | Supply a valid client token. |
| 403 | `{"error":"user_id does not match this API client"}` | Omit `user_id`; changing it cannot expand access. |
| 403 | `{"error":"This API client is limited to reading articles"}` | Reader tokens cannot access editorial writes or user/scheduling endpoints. |
| 404 | `{"error":"Article not found"}` | Detail ID is absent, archived, or outside the actor's regions. |
| 429 | `{"error":"Rate limit exceeded"}` | Honor `Retry-After` when present; retry with bounded exponential backoff and jitter. |
| 503 | `{"error":"API temporarily unavailable"}` | Shared rate-limit service is unavailable; retry with bounded backoff. |

Two rate limits protect the API:

- The shared **per-client** counter defaults to 120 requests per fixed calendar
  minute, across all Puma processes/hosts and caller IPs. It uses an atomic Redis
  counter, stores no raw token, and is always enforced in production. Operators
  can set `API_CLIENT_RATE_LIMIT_PER_MINUTE` to a positive integer. Its 429
  response includes `Retry-After` in seconds until the next minute. If Redis is
  unavailable, requests fail closed with 503.
- The existing `/api/` IP throttle defaults to 120 requests per minute when
  `RACK_ATTACK_ENABLED=true` (the production default), including invalid tokens.
  `RACK_ATTACK_API_LIMIT_PER_MINUTE` controls it. Its cache remains process-local,
  and its 429 response does not include `Retry-After`. Applications sharing an
  IP can hit this limit together.

Use timeouts and bounded retries for transient 5xx or network failures. Do not
retry invalid queries or credentials unchanged. HTTPS protects tokens in transit;
revocation and expiry bound credential lifetime. Readers cannot edit articles or
use an edit link to authenticate a browser into either CMS.

## Persisted article field reference

The following fields are returned in addition to the four derived fields above.
Names reflect `db/schema.rb`; types describe their Rails JSON representation.
Most columns allow null; preserve that
possibility in your client. JSON report structures evolve with the editorial
pipeline and should be consumed defensively.

| Field | JSON type | Meaning |
| --- | --- | --- |
| `hl_title` | string | Current editorial headline (searched). |
| `hl_meta` | string | Editorial meta line. |
| `hl_excerpt` | string | Editorial excerpt. |
| `full_content` | string | Stored article body; may contain HTML or editor JSON text. |
| `addresses` | array of strings or null | Rails-deserialized stored addresses; use as JSON, not YAML. Unlike the published-articles feed, these endpoints do not apply `addresses_list` normalization. |
| `fact_check_rating` | integer | Fact check rating assessment value. |
| `interest_rating` | integer | Interest rating assessment value. |
| `status` | string | Workflow status: `requires_editing`, `in_queue`, `published`, or `review`. |
| `picked_up_by_id` | integer | Local User ID for picked up by. |
| `submitted_by_id` | integer | Local User ID for submitted by. |
| `published_at` | ISO 8601 string | Publication workflow timestamp; do not infer release completion from this alone. |
| `created_at` | ISO 8601 string | Local record creation time. |
| `updated_at` | ISO 8601 string | Local record last update time. |
| `fact_matching` | integer | Fact matching assessment value. |
| `florid_rating` | integer | Florid rating assessment value. |
| `newsworthiness_rating` | integer | Newsworthiness rating assessment value. |
| `meta_description` | string | Stored meta description. |
| `synopsis` | string | Stored synopsis. |
| `author_name` | string | Stored author name. |
| `primary_tag` | string | Primary article tag. |
| `main_content` | string | Stored main article content. |
| `image_url` | string | Image url. |
| `image_caption` | string | Image caption. |
| `image_source` | string | Image source. |
| `metro` | string | Article metro/region; used for actor access checks. |
| `image_query_json` | object/array | Structured image query data/report. |
| `unsplash_images_json` | object/array | Structured unsplash images data/report. |
| `image_photographer` | string | Image photographer. |
| `image_source_url` | string | Image source url. |
| `image_refresh_status` | string | Image refresh status. |
| `wikimedia_images_json` | object/array | Structured wikimedia images data/report. |
| `archived` | boolean | Archive flag; archived articles are excluded. |
| `openai_queries` | string | Stored image-search queries as text (may encode JSON). |
| `gov_images_json` | object/array | Structured gov images data/report. |
| `image_ai_recommendations_json` | object/array | Structured image ai recommendations data/report. |
| `auto_selected_image_url` | string | Auto selected image url. |
| `auto_selected_image_source` | string | Auto selected image source. |
| `auto_selected_image_source_url` | string | Auto selected image source url. |
| `title` | string | Original/imported headline (searched). |
| `url` | string | Source/origin URL; CMS imports use hoodline-cms://articles/<reference>. |
| `text` | string | Original/imported source text. |
| `image_pipeline_state` | object/array | Structured image pipeline state data/report. |
| `assigned_editor_id` | integer | Local User ID for assigned editor. |
| `fact_matching_json` | object/array | Structured fact matching data/report. |
| `fact_matching_score` | integer | Fact matching score assessment value. |
| `uniqueness_json` | object/array | Structured uniqueness data/report. |
| `uniqueness_score` | integer | Uniqueness score assessment value. |
| `business_images_json` | object/array | Structured business images data/report. |
| `agency_images_json` | object/array | Structured agency images data/report. |
| `copyright_risk_score` | integer | Copyright risk score assessment value. |
| `hot_news_risk_score` | integer | Hot news risk score assessment value. |
| `publish_after` | ISO 8601 string | Earliest allowed publication time. |
| `research_json` | object/array | Structured research data/report. |
| `cms_synced_image_url` | string | Image URL last synchronized with the CMS. |
| `cms_sync_error` | string | Latest CMS sync error message. |
| `cms_sync_error_at` | ISO 8601 string | Time of latest CMS sync error. |
| `editing_locked_by_id` | integer | Local User ID for editing locked by. |
| `editing_lock_heartbeat_at` | ISO 8601 string | Editing lock heartbeat timestamp. |
| `published_by_id` | integer | Local User ID for published by. |
| `citation_fix_json` | object/array | Structured citation fix data/report. |
| `image_visible_description` | string | Description of visible image content. |
| `cms_created_at` | ISO 8601 string | Original CMS creation timestamp. |
| `ignored_at` | ISO 8601 string | Time article was ignored. |
| `freshness_score` | integer | Freshness score assessment value. |
| `classification_json` | object/array | Structured classification data/report. |
| `intent_tier` | string | Editorial classification tier. |
| `queued_by_id` | integer | Local User ID for queued by. |
| `image_photographer_url` | string | Image photographer url. |
| `image_editor_confirmed_at` | ISO 8601 string | Image editor confirmed timestamp. |
| `source_link_count` | integer | Stored source link count. |
| `image_editor_confirmed_by_id` | integer | Local User ID for image editor confirmed by. |
| `image_copyright_ack_at` | ISO 8601 string | Image copyright ack timestamp. |
| `image_copyright_ack_by_id` | integer | Local User ID for image copyright ack by. |
| `image_copyright_ack_url` | string | Image copyright ack url. |
| `image_copyright_risk_json` | object/array | Structured image copyright risk data/report. |
| `degenerate_flagged_at` | ISO 8601 string | Degenerate flagged timestamp. |
| `degenerate_reason` | string | Reason for degenerate-content flag. |
| `fact_editing_locked_by_id` | integer | Local User ID for fact editing locked by. |
| `fact_editing_lock_heartbeat_at` | ISO 8601 string | Fact editing lock heartbeat timestamp. |
| `image_origin_url` | string | Image origin url. |
| `released_at` | ISO 8601 string | Confirmed CMS release timestamp. |
| `release_attempts` | integer | Count of CMS release attempts. |
| `cms_scheduled_at` | ISO 8601 string | CMS scheduling timestamp. |
| `commentary_fix_json` | object/array | Structured commentary fix data/report. |
| `editorial_triage_json` | object/array | Structured editorial triage data/report. |
| `low_source_override_at` | ISO 8601 string | Low source override timestamp. |
| `low_source_override_by_id` | integer | Local User ID for low source override by. |
| `queued_at` | ISO 8601 string | Time article entered the publication queue. |
| `publication_urgency` | object/array | Structured publication urgency assessment. |
| `publication_urgent` | boolean | Publication urgency flag. |

`id` (integer) is also included as the local primary key. User ID fields are
local user references, not nested users or remote CMS user IDs.
