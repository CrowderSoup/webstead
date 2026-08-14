# Microsub Extensions

`webstead`'s Microsub server (`microsub/`, mounted at `/microsub`) implements the
[Microsub spec](https://indieweb.org/Microsub-spec) plus a small set of additive,
ignorable extensions. Any spec-compliant client that doesn't know about these
simply won't send the extra params and gets standard spec behavior back — nothing
here is required to use the server.

## Discovering supported extensions

`action=channels` responses include a `_webstead` block listing what's supported,
so scripts/clients can check capabilities without reading source:

```json
{
  "channels": [...],
  "_webstead": {
    "timeline_filters": ["kind", "category", "author", "source"]
  }
}
```

## Timeline filters

`action=timeline` (`GET`) accepts four extra, repeatable query params beyond the
base spec's `channel`/`before`/`after`/`limit`/`filter`. All are optional and can
be combined (combining acts as AND across param types, OR across repeated values
of the same param type):

| Param | Values | Behavior |
|---|---|---|
| `kind` | `like`, `repost`, `bookmark`, `reply`, `checkin`, `photo`, `video`, `audio` | Only entries matching one of the given kinds. Repeat the param for multiple kinds (OR). An unrecognized kind value returns an empty result rather than being silently ignored. |
| `category` | any tag/category string | Only entries tagged with one of the given categories (matched against the entry's parsed `category` property). Repeatable. |
| `author` | a profile URL | Only entries from one of the given authors. Repeatable — accepts multiple author URLs. |
| `source` | a feed/subscription URL | Only entries from one of the given source feeds. Repeatable — accepts multiple source URLs. |

### Examples

Only unread photos in a channel:

```
GET /microsub?action=timeline&channel=home&kind=photo&is_read=false
Authorization: Bearer <token>
```

Everything from a specific author, across kinds:

```
GET /microsub?action=timeline&channel=home&author=https://example.com/
Authorization: Bearer <token>
```

Multiple kinds at once (likes and reposts):

```
GET /microsub?action=timeline&channel=home&kind=like&kind=repost
Authorization: Bearer <token>
```

## Content search across entries

`action=search` (`GET` or `POST`) branches on whether a `channel` param is
present:

- **No `channel` param** — standard spec behavior: treats `query` as a URL/domain
  to discover feeds at, for building a new subscription.
- **`channel=<uid>` present** (including `channel=global` to search across every
  channel) — non-spec content search: filters existing entries using the same
  `kind`/`category`/`author`/`source` params as `timeline`, plus full-text `query`
  (tokenized, matched against each entry's indexed title/summary/content). At
  least one of `query`, `author`, `category`, `kind`, or `source` is required in
  this mode.

```
POST /microsub?action=search
Authorization: Bearer <token>
channel=home&query=strava&kind=checkin
```

Returns the same `{"items": [...], "paging": {...}}` shape as `timeline`.

## Rich activity entries

Entries whose source h-entry embeds an `h-activity` object (the shape
`strava_integration/importer.py`'s `_build_mf2` produces, and that this site's
own activity posts render — see `themes/webstead-default-2026/templates/blog/_activity_stats.html`)
carry an additive `activity` key in the entry's JF2 `data`, alongside the
regular h-entry properties:

```json
{
  "type": "entry",
  "name": "Morning Run",
  "activity": {
    "type": "activity",
    "activity-type": "Run",
    "name": "Morning Run",
    "track": "https://example.com/media/strava-1.gpx",
    "x-distance": "8368.6",
    "x-moving-time": "2531",
    "x-elapsed-time": "2600",
    "x-total-elevation-gain": "95.1",
    "x-average-speed": "3.3",
    "x-max-speed": "4.1",
    "x-average-heartrate": "152.3",
    "x-max-heartrate": "178",
    "x-start-latlng": "45.0,-122.0",
    "x-end-latlng": "45.01,-122.01",
    "x-kudos-count": "7"
  }
}
```

Notes for consuming clients:

- `activity` is entirely optional — only present on entries that had an
  `h-activity` embedded object to parse. Absence doesn't mean "not an
  activity post"; it means no structured activity data was found.
- Every `x-`-prefixed key is additive and ignorable — a client that doesn't
  recognize a given stat should just skip it, the same as any unknown JF2
  property. New stats may be added to this set over time without a version
  bump; don't assume the key list above is exhaustive or fixed.
- All numeric values are strings, in the source's native SI units (meters,
  meters/second, seconds) regardless of the publishing site's display-unit
  preference — convert on the client side if you need imperial units.
- `x-start-latlng`/`x-end-latlng` are `"lat,lng"` strings, not nested objects.
- `activity-type`/`name`/`track` are un-prefixed (not `x-`) since they're the
  three fields this shape has always had, predating the `x-*` extension
  properties — treat them the same as any other JF2 property.

## Implementation note

Both `timeline` and the content-search path share one filter implementation —
`apply_timeline_filters` in `microsub/utils.py` — so the two can't drift apart on
what a given filter param means.

The `activity` embedded object is parsed by `_mf2_activity_to_jf2` in
`microsub/feed_parser.py`, which passes through any `x-`-prefixed property
generically rather than requiring a matching allowlist entry here for every
stat `_build_mf2` produces — see that function's docstring for why.
