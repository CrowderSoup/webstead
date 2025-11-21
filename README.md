# Blog

This is a blog built with Django.

## Micropub and Webmention

This project ships with a simple Micropub server and Webmention endpoint so that you can publish posts from compatible IndieWeb clients and accept mentions from other sites.

- **Micropub endpoint:** `/micropub`
- **Media endpoint:** `/micropub/media`
- **Webmention endpoint:** `/webmention`

Micropub requests must include a bearer token. Tokens are validated against the IndieAuth token endpoint at `https://tokens.indieauth.com/token`, so clients should request their own tokens and send them via the `Authorization: Bearer ...` header (or `access_token` parameter). Requests without a valid token receive `401` responses.

The Micropub endpoint supports creating the following post types based on the Micropub vocabulary:

- Articles (`name` provided)
- Notes (no `name`)
- Photo posts (`photo` uploads or URLs)
- Likes (`like-of`)
- Reposts (`repost-of`)
- Replies (`in-reply-to`)

Webmentions are stored with their source and target URLs, along with an optional `wm-property` to mark likes, reposts, and replies. When you publish a post, outgoing Webmentions are automatically discovered and sent to any linked URLs (including `like-of`, `repost-of`, and `in-reply-to` targets).
