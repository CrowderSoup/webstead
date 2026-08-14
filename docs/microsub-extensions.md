# Microsub Extensions

`webstead`'s Microsub server (`microsub/`, mounted at `/microsub`) implements the
[Microsub spec](https://indieweb.org/Microsub-spec) plus a small set of additive,
ignorable extensions. Any spec-compliant client that doesn't know about these
simply won't send the extra params and gets standard spec behavior back — nothing
here is required to use the server.

For the design rationale behind these extensions, see
`docs/microsub-extensions-plan.md`.

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

## Implementation note

Both `timeline` and the content-search path share one filter implementation —
`apply_timeline_filters` in `microsub/utils.py` — so the two can't drift apart on
what a given filter param means.
