# Blog

This is a blog built with Django.

## Themes

The blog supports drop-in themes discovered from the `themes/` directory. Each theme must contain a `theme.json` metadata file plus `templates/` and `static/` folders:

```
themes/
  cool-theme/
    theme.json
    templates/
      core/index.html
      blog/post.html
    static/
      css/theme.css
```

`theme.json` can include `label`, `author`, `version`, and any extra metadata you want to surface. Example:

```json
{
  "label": "Cool Theme",
  "author": "ACME",
  "version": "1.0.0",
  "description": "Blue gradients and serif type."
}
```

How it works:

- Themes are discovered at runtime; adding a new folder under `themes/` makes it show up in the **Site Configuration** admin dropdown immediately.
- Selecting an active theme in admin clears template caches so overrides apply without restarting the app.
- Templates first resolve from `themes/<slug>/templates/` (matching the same relative paths as the default templates) and automatically fall back to the built-in app templates when missing.
- Static assets are collected from each theme under the `themes/<slug>/static/` prefix, so you can reference them with `{% static theme.static_prefix|add:"css/theme.css" %}`. Run `collectstatic` after adding new theme assets.
- The `theme` context processor exposes:
  - `theme.slug` / `theme.label`
  - `theme.template_prefix` (e.g., `themes/cool-theme/templates/`) for `{% include theme.template_prefix|add:"blog/post.html" ignore missing %}`
  - `theme.static_prefix` (e.g., `themes/cool-theme/static/`) for building asset URLs
  - `theme.metadata` for any extra fields defined in `theme.json`

To create a new theme:

1. Create `themes/<your-slug>/theme.json` with at least a `label`.
2. Add any template overrides under `themes/<your-slug>/templates/` using the same paths as the default templates.
3. Add theme assets under `themes/<your-slug>/static/` and reference them via `theme.static_prefix`.
4. Choose the theme from **Core → Site Configuration** in the admin.

### Uploading themes

Uploaded themes are unpacked to the container and mirrored to the configured static bucket (MinIO locally, DigitalOcean Spaces in production). Upload a `.zip` that contains `theme.json`, `templates/`, and optionally `static/` from **Themes → Theme manager** in the Django admin. After uploading you can edit any text-based theme file (HTML, CSS, JS, JSON, etc.) from the same section; updates are saved back to the bucket.

You can override where themes are stored on disk or in the bucket with `THEMES_ROOT` and `THEME_STORAGE_PREFIX` environment variables (defaults are `BASE_DIR/themes` and `themes/` respectively).

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
