# Mastodon Direct Integration Plan

**Goal:** Replace Brid.gy (for publishing) and granary.io (for feed ingestion) with a direct Mastodon integration built into Webstead. This gives full control over publishing content to Mastodon and backfeeding Mastodon interactions (likes, boosts, replies, mentions, notifications) back into Webstead.

---

## Overview

The integration is organised as a new `mastodon` Django app with its own models, Celery tasks, OAuth flow, and admin UI. It hooks into the existing blog, microsub, and micropub machinery rather than replacing it.

### What changes
- **Publishing:** Instead of sending a webmention to `brid.gy/publish/mastodon`, Webstead calls the Mastodon API directly using an OAuth access token. The `bridgy_publish_mastodon` site configuration flag is retired.
- **Feed ingestion:** Instead of granary.io converting your Mastodon home timeline to an RSS feed that a Microsub subscription polls, a scheduled Celery task polls the Mastodon API directly and writes `Entry` objects into a user-chosen Microsub channel.
- **Backfeed:** Instead of Brid.gy polling for interactions, a scheduled Celery task fetches Mastodon notifications and converts them to webmentions (for likes/boosts on your posts) and Microsub entries (for replies/mentions/all notifications).

---

## New App: `mastodon/`

Create a new first-party Django app. Add it to `INSTALLED_APPS` and to Celery's autodiscover list.

```
mastodon/
├── __init__.py
├── apps.py
├── models.py
├── views.py          # OAuth flow only
├── urls.py
├── tasks.py
├── formatting.py     # Post → toot content conversion
├── client.py         # Thin wrapper around Mastodon.py
├── migrations/
└── templates/
    └── mastodon/
        ├── connect.html
        └── settings.html
```

---

## Models (`mastodon/models.py`)

### `MastodonApp`
Stores the OAuth application registration for a given instance. Created automatically during the OAuth flow if it doesn't exist for that instance URL.

| Field | Type | Notes |
|---|---|---|
| `instance_url` | `URLField(unique=True)` | e.g. `https://mastodon.social` |
| `client_id` | `CharField` | From Mastodon app registration |
| `client_secret` | `CharField` | From Mastodon app registration |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

### `MastodonAccount`
The connected Mastodon account. Effectively a singleton in practice (one account per Webstead installation), but modelled as a normal row to keep the door open for multi-account.

| Field | Type | Notes |
|---|---|---|
| `app` | `ForeignKey(MastodonApp)` | Which instance |
| `access_token` | `CharField` | OAuth access token |
| `account_id` | `CharField` | Mastodon account ID |
| `username` | `CharField` | e.g. `aaron@mastodon.social` |
| `display_name` | `CharField` | |
| `avatar_url` | `URLField(blank=True)` | |
| `is_active` | `BooleanField(default=True)` | |
| `timeline_channel` | `ForeignKey(microsub.Channel, null=True, blank=True, related_name='mastodon_timeline_source')` | Which Microsub channel receives the home timeline |
| `notifications_channel` | `ForeignKey(microsub.Channel, null=True, blank=True, related_name='mastodon_notifications_source')` | Which Microsub channel receives notifications |
| `last_timeline_id` | `CharField(blank=True)` | Mastodon status ID for pagination (fetch-since) |
| `last_notification_id` | `CharField(blank=True)` | Mastodon notification ID for pagination |
| `max_toot_chars` | `PositiveIntegerField(default=500)` | Fetched from instance API during OAuth |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

### `MastodonSyndicationDefault`
Per-post-kind defaults controlling whether new posts of that kind are automatically published to Mastodon.

| Field | Type | Notes |
|---|---|---|
| `post_kind` | `CharField(choices=Post.KIND_CHOICES, unique=True)` | e.g. `note`, `article`, `like` |
| `publish` | `BooleanField(default=False)` | Default: don't publish |

Pre-populate on migration with `publish=True` for `note` and `article`, `False` for all others.

### `MastodonPost`
Links a Webstead `Post` to the Mastodon status that was created for it.

| Field | Type | Notes |
|---|---|---|
| `post` | `OneToOneField(blog.Post, related_name='mastodon_post')` | |
| `mastodon_id` | `CharField` | Status ID on the Mastodon instance |
| `mastodon_url` | `URLField` | Public URL of the toot |
| `published_at` | `DateTimeField(auto_now_add=True)` | |

### Changes to `blog.Post`
Add one field:

```python
mastodon_syndicate = models.BooleanField(
    null=True, blank=True,
    help_text="Override Mastodon syndication for this post. "
              "Null = use per-kind default."
)
```

`null` = use the `MastodonSyndicationDefault` for this post's `kind`.
`True` = always publish, regardless of default.
`False` = never publish, regardless of default.

### Changes to `core.SiteConfiguration`
Remove `bridgy_publish_mastodon`. Leave `bridgy_publish_bluesky`, `bridgy_publish_flickr`, `bridgy_publish_github` untouched — those services still go through Brid.gy.

---

## Dependency

Add `Mastodon.py` to `pyproject.toml`:

```toml
"Mastodon.py>=1.8",
```

This is the canonical Python client for the Mastodon API. It handles OAuth, pagination, and rate limit headers cleanly.

---

## OAuth Flow

### Step 1 — Enter instance URL
Admin navigates to `/admin/mastodon/` → clicks "Connect Mastodon" → form asking for instance URL (e.g. `https://mastodon.social`).

### Step 2 — App registration (`/mastodon/auth/start/`)
`POST` handler:
1. Look up or create a `MastodonApp` for that instance URL.
2. If not yet registered, call `Mastodon.create_app()` to register a new OAuth app with the instance. Store `client_id` + `client_secret`.
3. Construct the Mastodon OAuth authorisation URL with scopes `read write` and redirect URI pointing to `/mastodon/auth/callback/`.
4. Store `instance_url` in the Django session.
5. Redirect the browser to the Mastodon authorisation page.

### Step 3 — Callback (`/mastodon/auth/callback/`)
`GET` handler (Mastodon redirects here with `?code=...`):
1. Retrieve `instance_url` from session.
2. Exchange the code for an access token via `Mastodon.log_in()`.
3. Fetch account credentials from the API to populate `username`, `display_name`, `avatar_url`, `account_id`.
4. Create (or update) the `MastodonAccount` record.
5. Redirect to `/admin/mastodon/` with a success message.

### Disconnect (`/admin/mastodon/disconnect/`)
Revokes the access token via the Mastodon API, then deletes the `MastodonAccount` and (optionally) the `MastodonApp` record.

---

## Celery Tasks (`mastodon/tasks.py`)

### `publish_post_to_mastodon(post_id)` — triggered, not scheduled

**Triggered by:** `micropub/tasks.py → dispatch_webmentions()` (or a post-save signal) when syndication should occur.

**Logic:**
1. Load the `Post`. Check it's not deleted and is published.
2. Determine whether to syndicate:
   - If `post.mastodon_syndicate` is not null, use that value.
   - Otherwise, look up `MastodonSyndicationDefault` for `post.kind`.
   - If no default found, do not publish.
3. Check that no `MastodonPost` already exists for this post (idempotency).
4. Load the active `MastodonAccount`. If none, abort.
5. Build toot content via `mastodon.formatting.format_post(post)` (see Formatting section below).
6. Call `mastodon_client.status_post(status=content, ...)`.
7. Create a `MastodonPost` record with the returned status ID and URL.
8. Optionally: update `post.mf2` with a `syndication` entry pointing at the toot URL.

**Retries:** 3× with 60-second delays (same pattern as other tasks in the codebase).

---

### `poll_mastodon_timeline()` — scheduled every 15 minutes

**Triggered by:** Celery Beat (`poll-mastodon-timeline` beat entry).

**Logic:**
1. Load the active `MastodonAccount`. If none or no `timeline_channel` set, abort.
2. Fetch home timeline from Mastodon API, using `since_id=account.last_timeline_id` to get only new statuses.
3. For each status (newest-last to preserve order):
   a. Convert to JF2 via `mastodon.client.status_to_jf2(status)`.
   b. Deduplicate: skip if an `Entry` with matching `jf2['url']` already exists in this channel's subscription scope. Since these are direct Mastodon statuses (not from a subscription URL), use a synthetic "mastodon-timeline" subscription or a direct channel insert with a `mastodon_id` marker.
   c. Create `microsub.Entry` with `channel=account.timeline_channel`, `data=jf2`, `published=status.created_at`.
4. Update `account.last_timeline_id` to the ID of the most recent status fetched.

**Note on Microsub Entry storage:** Timeline entries are not associated with a `Subscription` (there is no RSS feed URL). We have two options:
- **Option A (recommended):** Create a synthetic `Subscription` with `url="mastodon:timeline:{account.account_id}"` in the channel. This lets existing microsub views (timeline, read, remove) work without modification.
- **Option B:** Add an optional `FK` to `MastodonAccount` on `Entry` and handle in the microsub view layer.

Option A requires the least disruption to existing code.

---

### `poll_mastodon_notifications()` — scheduled every 15 minutes

**Triggered by:** Celery Beat (`poll-mastodon-notifications` beat entry).

**Logic:**
1. Load the active `MastodonAccount`. If none, abort.
2. Fetch notifications from Mastodon API, `since_id=account.last_notification_id`.
3. For each notification, dispatch to the appropriate handler based on `notification.type`:

| Type | Action |
|---|---|
| `favourite` | If the favourited status links back to a Webstead post URL, create an incoming webmention (call `verify_and_update_webmention` or create a `Webmention` object directly). Also create an Entry in `notifications_channel`. |
| `reblog` | Same as favourite, but `wm-property=repost-of`. |
| `mention` | Create an Entry in `notifications_channel` with the mention content. Also create an incoming `Comment` or webmention if the mention is a reply to a Webstead post. |
| `follow` | Create a lightweight Entry in `notifications_channel`. |

4. Update `account.last_notification_id`.

**Matching statuses to Webstead posts:** When a favourite/reblog notification arrives, check whether `notification.status.url` appears as `post.syndication` data in `mf2`, or whether the toot links back to a Webstead post URL. Use the `MastodonPost.mastodon_id` table as the primary lookup: `MastodonPost.objects.filter(mastodon_id=notification.status.id)`.

---

## JF2 Conversion (`mastodon/client.py`)

`status_to_jf2(status)` converts a Mastodon.py `Status` object to a JF2 dict compatible with the existing `microsub.Entry.data` format:

```python
{
    "type": "entry",
    "name": "",                          # toots don't have titles
    "content": {"html": status.content, "text": strip_html(status.content)},
    "url": status.url,
    "published": status.created_at.isoformat(),
    "author": {
        "type": "card",
        "name": status.account.display_name,
        "url": status.account.url,
        "photo": status.account.avatar,
    },
    # Include media attachments
    "photo": [a.url for a in status.media_attachments if a.type == "image"],
    "video": [a.url for a in status.media_attachments if a.type == "video"],
    # Reblog / boost handling
    "repost-of": status.reblog.url if status.reblog else None,
    # Content warning → summary
    "summary": status.spoiler_text or None,
}
```

---

## Post Formatting (`mastodon/formatting.py`)

`format_post(post)` builds the toot text from a Webstead `Post`:

- **note:** Use `post.content` directly (markdown-stripped to plain text). Truncate to `account.max_toot_chars - len(canonical_url) - 3` chars, appending "… {url}" if truncation was needed. `format_post()` accepts the account's `max_toot_chars` as a parameter so it can be used consistently across all kinds.
- **article:** Use `post.title` + a newline + the post's canonical URL.
- **like / bookmark / repost:** Use a short phrase + the target URL (e.g. "♥ {like_of}" or "↩ {in_reply_to}").
- **photo:** Caption (content) + attached media (upload the image file to Mastodon first using `media_post()`).
- All kinds: append relevant tags from `post.tags` as hashtags if they fit within the character limit.

For posts that are replies (`in_reply_to` pointing at a known `MastodonPost`), set `in_reply_to_id` on the API call.

---

## Admin UI

### `/admin/mastodon/` — Mastodon Settings page

Shows:
- **Connection status:** Either "Not connected" + "Connect" button, or the connected account's display name, instance, and avatar + "Disconnect" button.
- **Per-kind defaults table:** A row per post kind with a toggle (enabled/disabled). Saved via a form `POST`.
- **Timeline channel selector:** Dropdown of existing Microsub channels (or "None — don't ingest timeline"). Saved inline.
- **Notifications channel selector:** Same.
- **Manual sync button:** Triggers `poll_mastodon_timeline` and `poll_mastodon_notifications` as immediate Celery tasks.

### `/admin/mastodon/connect/` — Connect form

Simple form: one field for instance URL (e.g. `https://mastodon.social`). Submit → POST to `/mastodon/auth/start/`.

This page is login-protected (same `@login_required` + staff check as all other admin views).

### Site Admin nav

Add a "Mastodon" link to the site admin sidebar navigation, alongside the existing "Microsub" and "Settings" links.

---

## Post Editor Changes (`site_admin/templates/`)

On the post edit form, add a Mastodon syndication control alongside the existing Bridgy publish checkboxes. Since the existing pattern is a simple checkbox, we add a three-state select:

```
Mastodon:  [Default ▾]   (options: Default / Publish / Don't publish)
```

This maps to `post.mastodon_syndicate = None / True / False`.

---

## Wiring Into the Publish Flow

### Where `publish_post_to_mastodon` gets dispatched

In `micropub/tasks.py → dispatch_webmentions()`, there's already logic to dispatch Bridgy Publish webmentions. After that block, add:

```python
from mastodon.tasks import publish_post_to_mastodon

if should_syndicate_to_mastodon(post):
    publish_post_to_mastodon.delay(post.id)
```

`should_syndicate_to_mastodon(post)` checks:
1. Is there an active `MastodonAccount`?
2. Resolve `post.mastodon_syndicate` vs `MastodonSyndicationDefault` (as described in the task above).

The same dispatch should happen for posts created via the admin post editor (currently `blog/views.py` triggers `dispatch_webmentions`; the same hook applies).

---

## Celery Beat Schedule Changes (`config/celery.py` / `config/settings.py`)

Add two new beat entries:

```python
"poll-mastodon-timeline": {
    "task": "mastodon.tasks.poll_mastodon_timeline",
    "schedule": 900.0,  # every 15 minutes, matching microsub poll cadence
},
"poll-mastodon-notifications": {
    "task": "mastodon.tasks.poll_mastodon_notifications",
    "schedule": 900.0,
},
```

Add `"mastodon"` to the Celery autodiscover list in `config/celery.py`.

---

## URL Routing

### New `mastodon/urls.py`

```python
urlpatterns = [
    path("mastodon/auth/start/", views.oauth_start, name="mastodon_oauth_start"),
    path("mastodon/auth/callback/", views.oauth_callback, name="mastodon_oauth_callback"),
]
```

### New entries in `site_admin/urls.py`

```python
path("mastodon/", mastodon_views.settings, name="admin_mastodon"),
path("mastodon/connect/", mastodon_views.connect, name="admin_mastodon_connect"),
path("mastodon/disconnect/", mastodon_views.disconnect, name="admin_mastodon_disconnect"),
path("mastodon/sync/", mastodon_views.manual_sync, name="admin_mastodon_sync"),
```

Include `mastodon.urls` in `config/urls.py`.

---

## Removing Brid.gy Mastodon

Once the new integration is working:

1. Remove `bridgy_publish_mastodon = models.BooleanField(...)` from `core.SiteConfiguration` (with a migration).
2. Remove the `brid.gy/publish/mastodon` entry from `BRIDGY_PUBLISH_TARGETS` in `micropub/webmention.py`.
3. Remove the corresponding setting from the site settings admin template.

The other three Bridgy targets (Bluesky, Flickr, GitHub) are unaffected.

---

## Implementation Phases

### Phase 1 — Foundation
- Create `mastodon/` app (scaffold only, no logic yet)
- Add models: `MastodonApp`, `MastodonAccount`, `MastodonSyndicationDefault`, `MastodonPost`
- Add `mastodon_syndicate` nullable bool field to `blog.Post`
- Add `Mastodon.py` to `pyproject.toml`
- Write and run migrations
- Add app to `INSTALLED_APPS` and Celery autodiscover

### Phase 2 — OAuth Connection
- Implement `mastodon/views.py`: `oauth_start`, `oauth_callback`
- Implement `mastodon/client.py`: thin wrapper initialising `Mastodon.py` from `MastodonAccount`
- Add admin views: `settings`, `connect`, `disconnect`
- Add admin templates for connection UI
- Wire into admin sidebar nav
- Add mastodon URL patterns to `config/urls.py` and `site_admin/urls.py`

### Phase 3 — Publishing
- Implement `mastodon/formatting.py`: `format_post()`
- Implement `mastodon/tasks.py`: `publish_post_to_mastodon()`
- Hook into `micropub/tasks.py → dispatch_webmentions()`
- Add three-state Mastodon toggle to post edit form
- Add per-kind defaults table to admin Mastodon settings UI

### Phase 4 — Timeline Ingestion
- Implement `status_to_jf2()` in `mastodon/client.py`
- Implement `poll_mastodon_timeline()` task
- Create synthetic `Subscription` approach for timeline entries
- Add beat schedule entry
- Add timeline channel selector to admin Mastodon settings UI

### Phase 5 — Backfeed & Notifications
- Implement `poll_mastodon_notifications()` task
- Handle all four notification types (favourite, reblog, mention, follow)
- Create webmentions for likes/boosts matching `MastodonPost` records
- Create microsub entries in notifications channel
- Add beat schedule entry
- Add notifications channel selector to admin Mastodon settings UI

### Phase 6 — Cleanup
- Remove `bridgy_publish_mastodon` from `SiteConfiguration` model + migration
- Remove `brid.gy/publish/mastodon` from Bridgy dispatch logic
- Remove Mastodon toggle from site settings template
- Update `README.md` / `AGENTS.md` to document the new integration

---

## Resolved Decisions

- **Character limit:** Fetch `max_toot_chars` from the instance API during the OAuth callback and store it on `MastodonAccount`. Truncation logic uses this value, falling back to 500 if unavailable.

- **Media uploads for photos:** Handled inline inside `publish_post_to_mastodon` — upload media attachments first, then post the status in sequence. No separate chained task.

- **Visibility:** Default to `public` for all post kinds. This can be revisited later if per-kind visibility control is needed.

- **Content warnings:** Posts tagged `cw` automatically set Mastodon's `spoiler_text`. The first line of `post.content` is used as the CW text, with the remainder as the toot body. The `cw` tag itself is not included in the hashtag list.

- **Granary removal:** Once Phase 4 (timeline ingestion) is live and verified, the granary.io Microsub subscription can be deleted manually from the reader UI. The synthetic Mastodon timeline subscription replaces it.

- **Multiple Mastodon accounts:** The model supports it via `MastodonAccount.is_active`, but the UI and task dispatch assume a single active account for now.
