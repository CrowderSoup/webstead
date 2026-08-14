# Microsub / Activity Feed Extensions Plan

**Goal:** Push webstead's Microsub server and activity-post publishing past the base spec — expose filtering that already exists but is invisible, and make activity/checkin/photo/reply posts render richly for anyone subscribing to the site, without breaking any spec-compliant client that doesn't know about the extensions.

## Sources consulted

- [Microsub spec](https://indieweb.org/Microsub-spec) (indieweb.org) — actions: `channels`, `timeline`, `follow`, `unfollow`, `mute`, `unmute`, `block`, `unblock`, `search`, `preview`, plus `mark_read`/`mark_unread` as timeline sub-operations.
- [Micropub](https://www.w3.org/TR/micropub/) (W3C Recommendation) — h-entry is mandatory; `mp-` is the reserved prefix for vendor extensions; extension experiments are tracked at indieweb.org/Micropub-extensions.
- [Post Type Discovery](https://ptd.spec.indieweb.org/) (W3C Note) — the standardized algorithm for classifying entries (event, rsvp, repost, like, reply, video, photo, note, article). Explicitly states other types (like "activity") may emerge from "convergence of publishing patterns" but aren't standardized yet — degradation for non-standard types is our problem to solve, not the spec's.
- [JSON Feed](https://www.jsonfeed.org/) — blessed `_`-prefixed extension convention, used here as a model for "additive, ignorable" custom keys.
- Aperture (Aaron Parecki's reference Microsub server) — prior art for going beyond spec: kind-only filtering, demo mode, per-channel read-tracking toggles, unread dot-vs-count. Confirms filtering-by-kind is a well-trodden, low-risk extension.

**Guiding principle (per conversation):** stay within `mp-`/`x-`-style vendor-extension conventions — additive fields and params only, so any spec-compliant client (Monocle, Together, Indigenous, etc.) degrades gracefully and ignores what it doesn't understand. No first-party reader UI is planned; richness has to land in the JF2/mf2/content payload itself.

---

## Current state (confirmed in code)

- `microsub/views.py` `_get_timeline`/`_post_timeline` (~595-653) **already accepts** `source`, `author`, `category`, and `kind` filter params — this is undocumented and unused by anything (no UI, no docs, no client). `_content_search_qs` (444-505) duplicates this and adds full-text `query` tokenization, reachable via `action=search` when a `channel` param is present (dispatch logic at 679-689).
- `Entry` (`microsub/models.py`) already denormalizes `kind_like/repost/bookmark/reply/checkin/photo/video/audio`, and maintains a real category/search index (`EntryCategory`, `EntrySearchToken`) synced on every save.
- Strava activities never touch Microsub — `strava_integration/importer.py:import_activity` writes straight to `blog.Post(kind=ACTIVITY)`. `_build_mf2` (importer.py:33-45) only captures `activity-type`, `name`, and a `track` GPX link.
- `client.get_activity()` (strava_integration/client.py:128-130) fetches Strava's full `DetailedActivity` — distance, moving_time, elapsed_time, total_elevation_gain, average_speed/max_speed, average_heartrate/max_heartrate (when available), start_latlng/end_latlng, kudos_count, etc. — none of this reaches `_build_mf2` today.
- `post.content` for activity posts is `activity.get("description") or ""` (importer.py:130) — Strava descriptions are frequently empty, so activity posts often publish with **no body text at all**.
- No reply-context, checkin venue enrichment, or notification grouping exists anywhere in `microsub/` today.

---

## Phase 1 — Expose existing timeline filters

Lowest-risk, mostly-done work: make the filtering that already exists actually usable.

1. **`docs/microsub-extensions.md`** — document the non-spec `timeline` params (`kind`, `category`, `author`, `source`) and the `search`+`channel` content-search path, with example requests. Mirrors the level of detail in `docs/mastodon-integration-plan.md`.
2. **Capability advertisement** — add a `_webstead` block to the `action=channels` response listing supported extension params, so scripts/clients can discover them without reading source:
   ```json
   {"channels": [...], "_webstead": {"timeline_filters": ["kind", "category", "author", "source"]}}
   ```
3. **Admin debug view** — a minimal page under `site_admin/templates/site_admin/microsub/` (e.g. `channel_timeline.html`) that lists an `Entry` queryset with filter controls for kind/category/author/read-state. Not a reader — just enough to verify the filters work and use them day-to-day. Reuses `_visible_entries_qs` / the same filtering helpers as `_content_search_qs`, called directly from a Django view rather than the JSON API.

---

## Phase 2 — Rich activity markup

Target: someone subscribing to your h-feed sees a real activity card, not a bare title.

1. **Content summary generation** — when `activity.get("description")` is empty (the common case), synthesize a plain-text/HTML summary into `post.content`, e.g. *"Ran 5.2 mi in 42:11 (8:07/mi pace), 312 ft elevation gain."* This is the fallback that makes the post readable in any h-entry-only client, per your "rich content text + custom properties" decision.
2. **Extend `_build_mf2`** with additional properties, all additive/ignorable for clients that don't recognize `h-activity`:
   - Distance, duration (moving + elapsed), average pace/speed
   - Elevation gain (and, if you want a profile, the altitude stream already fetched in `_attach_gpx` — currently discarded after GPX generation)
   - Structured geo: `start_latlng`/`end_latlng` inline (not just the GPX attachment link)
   - Effort: `average_heartrate`/`max_heartrate` when Strava provides them (`has_heartrate` flag)
3. **Open question to resolve during implementation:** unit preference (imperial vs metric) — needs a site-level or per-post setting; Strava's API returns metric (meters, m/s) natively.
4. Apply the same content-summary + property pattern to `checkin` and `photo` kinds only after Phase 2 activity work proves out the pattern (per your "checkin/photo too" answer, sequenced after activity).

---

## Phase 3 — Reply-context, checkin/photo richness, notification grouping

No strict order requested beyond "after Phase 2" — sequence these as convenient.

### 3a. Reply-context (ingest-time, cached)
When an incoming feed `Entry` is `kind_reply` with an `in-reply-to` URL, fetch and parse the parent (mf2py is already a dependency) at ingest time, and cache a summary (parent author name/photo/url + snippet) under an additive key in `Entry.data` (e.g. `_reply_context`, following the JSON Feed underscore-extension convention). Best run as a Celery task fired after entry creation, not inline in the polling task, so a slow/failed parent fetch never blocks ingestion — on failure, just leave `_reply_context` absent (already-graceful degradation).

### 3b. Checkin venue data + photo galleries
Narrower than originally scoped — a deeper read (with the existing test suite) showed most of this already works:
- **Checkin:** already works. `test_checkin_embedded_hcard` (`microsub/tests/test_feed_parser.py:185`) confirms venue name/geo already survives `_hentry_to_jf2` → `_mf2_embedded_to_jf2` (feed_parser.py:254-260), and `normalize_entry_data` never touches the `checkin` key so it passes through unmodified. First step here is a regression-guard test at the `_store_entries` level (nothing currently tests this past the feed-parser unit level), not new parsing code — only write parsing code if that test reveals a gap.
- **Photo:** `test_photo_multiple` (`test_feed_parser.py:228`) confirms multi-photo galleries already work — `_hentry_to_jf2`'s photo/video/audio handling already loops over all values, not just the first. The `photo_vals[0]` truncation at feed_parser.py:185-188 (and 356-357/384-387) is for **card avatars** (author photo, feed icon, venue photo) — correct to take one there. The real, confirmed bug is narrower: `test_photo_dict_value` (`test_feed_parser.py:236`) shows `{"value": "...jpg", "alt": "..."}` collapses to a bare URL, losing alt text — `_url_vals` (feed_parser.py:263-272) keeps only the URL, and `ensure_string_list` (utils.py:65-78) would strip a dict form even if one reached it. Work: add `ensure_photo_list` in `utils.py` (preserves `{"value", "alt"}` dict form), wire into `normalize_entry_data`/`infer_kind_flags`, and update the photo branch of `feed_parser.py`'s mf2-key loop to keep `alt` when present.

### 3c. Notification grouping/collapsing
Confirmed built: `mastodon_integration/tasks.py:poll_mastodon_notifications` (not `mastodon/` as the original integration plan doc names it) already polls every 15 minutes and converts `favourite`/`reblog`/`mention`/`follow`/`poll` notifications to JF2 via `_notification_to_jf2` (tasks.py:475-581), storing them through `microsub.views._store_entries`. To group: in the `favourite`/`reblog` branch of the loop (tasks.py:432-462), before calling `_store_entries`, look up an existing recent (e.g. 24h) `Entry` in `notifications_channel` whose `data["like-of"]`/`data["repost-of"]` matches the same target status URL, and increment a count in its `data` (e.g. `_count`) instead of creating a new one.

---

## Sequencing

| Phase | Work | Status |
|---|---|---|
| 1 | Document + advertise existing timeline filters; admin debug view | Ready to start — mostly exposure, not new logic |
| 2 | Rich activity markup + content summaries | Ready to start — all source data confirmed available via `client.get_activity()` |
| 3a | Reply-context (ingest-time, cached) | Ready to start |
| 3b | Checkin venue data + photo galleries | Ready to start — photo truncation confirmed as a two-line bug fix in `feed_parser.py`, not a design gap |
| 3c | Notification grouping | Ready to start — `mastodon_integration.tasks.poll_mastodon_notifications` confirmed built |

## Open questions to resolve before/during implementation

- Unit preference (imperial/metric) for activity properties and content summaries.
- Exact property names for the extended `h-activity` block (avoid colliding with any future real PTD "activity" type).
