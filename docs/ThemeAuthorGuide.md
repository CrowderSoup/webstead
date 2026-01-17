# Theme Author Guide

This guide is for designers and developers building themes. It explains how themes are structured, how they interact with the system, and how to design themes that are flexible, safe, and pleasant to use.

This guide is intentionally practical and example-driven. For strict rules and validation requirements, refer to the Theme Spec.

---

## What Is a Theme?

A theme controls **presentation only**. It defines:

- HTML templates
- CSS and other static assets
- Optional, user-configurable theme settings

A theme **does not**:

- Contain business logic
- Define content models
- Store data
- Change application behavior

If something affects how data is fetched, stored, or processed, it does not belong in a theme.

---

## Theme Directory Basics

Every theme lives in its own directory under `themes/`:

```
themes/
  └── my-theme/
      ├── theme.json
      ├── static/
      └── templates/
```

- The directory name is the theme's **slug**
- The theme is identified and discovered via `theme.json`
- Templates and static assets are only used when the theme is active

You can safely assume:

- Only one theme is active at a time
- Templates are never mixed between themes

---

## `theme.json`: Your Theme's Contract

`theme.json` is the public contract between your theme and the system.

Use it to:

- Name and describe your theme
- Declare configurable settings
- Expose metadata to templates

Keep it small and stable. Changing keys later can break existing sites.

### Minimal example

```json
{
  "label": "My Theme"
}
```

This is a valid theme.

---

## Theme Settings: Designing for Flexibility

Theme settings let site owners customize presentation without editing code.

Good candidates for settings:

- Colors
- Font stacks
- Layout widths
- Toggles for optional UI elements

Bad candidates:

- Content
- Feature flags
- Anything that changes application behavior

### Defining settings

```json
{
  "settings": {
    "fields": {
      "accent_color": {
        "type": "color",
        "label": "Accent color",
        "default": "#cc3f2e"
      }
    }
  }
}
```

Guidelines:

- Always provide sensible defaults
- Prefer fewer settings over more
- Settings should feel safe to change

---

## Using Settings in Templates

When your theme is active, settings are available as `theme.settings`.

```html
<style>
  :root {
    --accent: {{ theme.settings.accent_color|default:"#cc3f2e" }};
  }
</style>
```

Best practices:

- Treat settings as optional
- Always guard with defaults
- Centralize CSS variables instead of scattering settings throughout templates

---

## Template Context: What's Available

Templates receive a mix of standard Django context plus site- and theme-specific data.

### Standard Django context

Available on most views (from Django's built-in context processors):

- `request` (the current request object)
- `user` (the authenticated user, if any)
- `messages` (Django messages framework)

### Site configuration context

Provided by the `core.context_processors.site_configuration` context processor:

- `settings` (the `SiteConfiguration` object)
- `menu_items` (main menu items or `None`)
- `footer_menu_items` (footer menu items or `None`)
- `feed_url` (absolute URL to the posts feed or `None`)
- `site_author_hcard` (the primary h-card object or `None`)
- `site_author_display_name` (resolved display name string)
- `og_default_image` (absolute URL string for the default Open Graph image)

### Theme context

Provided by the `core.context_processors.theme` context processor:

- `active_theme` (the active theme object or `None`)
- `theme.slug` (the theme slug)
- `theme.label` (the human-friendly label)
- `theme.metadata` (all extra keys from `theme.json`)
- `theme.settings` (resolved settings with defaults applied)
- `theme.settings_schema` (raw settings schema from `theme.json`)
- `theme.template_prefix` (prefix for `{% include %}` paths)
- `theme.static_prefix` (prefix for `{% static %}` assets)

### Quick examples

```html
<title>{{ settings.site_name }}{% if settings.tagline %} - {{ settings.tagline }}{% endif %}</title>
```

```html
<link rel="alternate" type="application/rss+xml" href="{{ feed_url }}">
```

```html
{% if site_author_hcard %}
  <p>By {{ site_author_display_name }}</p>
{% endif %}
```

```html
<link rel="stylesheet" href="{% theme_static 'css/theme.css' %}">
```

Notes:

- Context can vary by view; always guard optional fields.
- When in doubt, check the default templates for usage patterns.

---

## Blog Post Templates: Context and Model Fields

The single post template lives at `blog/post.html` and receives:

- `post` (the `Post` model instance)
- `activity` (activity summary dict for activity posts, or `None`)
- `activity_photos` (photo attachments for activity posts, or `[]`)
- Standard context listed above (site configuration, theme settings, etc.)

### `Post` fields and helpers

Core fields:

- `post.title`, `post.slug`, `post.content`
- `post.kind` (one of `article`, `note`, `photo`, `activity`, `like`, `repost`, `reply`)
- `post.published_on`, `post.deleted`
- `post.like_of`, `post.repost_of`, `post.in_reply_to` (URLs for interaction posts)
- `post.mf2` (mf2 JSON payload, if provided)

Relations and helpers:

- `post.author` (user; may be `None`)
- `post.author.hcards` (prefetched; use for author display)
- `post.tags` (many-to-many; use `post.tags.all`)
- `post.attachments` (generic relation; use `post.attachments.all`)
- `post.photo_attachments` (attachments filtered to `role="photo"`)
- `post.gpx_attachment` (first attachment with `role="gpx"` or `None`)
- `post.html()` (markdown rendered to safe HTML)
- `post.summary()` (plain-text excerpt, ~500 chars)
- `post.get_absolute_url()`

### Activity context

When `post.kind == "activity"`:

- `activity.name` (string)
- `activity.track_url` (URL to a GPX track or external activity link)
- `activity_photos` contains `Attachment` objects for photo role

### Interaction context

When `post.kind` is `like`, `repost`, or `reply`, the view sets:

- `post.interaction.kind`
- `post.interaction.label` (e.g., "Liked", "Reposted", "Replying to")
- `post.interaction.target_url`
- `post.interaction.target` (dict with `title`, `summary_text`, `summary_excerpt`, etc., when available)
- `post.interaction.show_content` (bool; hide default "Liked {url}" text)

---

## Page Templates: Context and Model Fields

The page template lives at `core/page.html` and receives:

- `page` (the `Page` model instance)
- Standard context listed above (site configuration, theme settings, etc.)

### `Page` fields and helpers

- `page.title`, `page.slug`, `page.content`
- `page.published_on`
- `page.author` (user; may be `None`)
- `page.author.hcards` (prefetched; use for author display)
- `page.attachments` (generic relation; use `page.attachments.all`)
- `page.html()` (markdown rendered to safe HTML)

### Attachments and assets

Both posts and pages use `Attachment` objects with linked `File` assets:

- `attachment.asset.file.url` (file URL)
- `attachment.asset.alt_text`
- `attachment.asset.caption`
- `attachment.asset.kind` (`image`, `doc`, `video`)
- `attachment.role` (theme-defined role like `hero`, `inline`, `gallery`)
- `attachment.sort_order`

---

## Templates: Structure and Expectations

Themes provide templates under `templates/`. The exact structure is flexible, but some conventions exist.

Common patterns:

- `base.html` defines the site shell
- Content templates extend `base.html`
- Errors like `500.html` should fail gracefully

Example:

```html
{% extends "base.html" %}

{% block content %}
  <article>
    {{ page.content|safe }}
  </article>
{% endblock %}
```

Guidelines:

- Keep templates readable
- Avoid deeply nested logic
- Prefer composition over duplication

---

## Static Assets

Static assets live under `static/` inside your theme.

Use the `theme_static` template tag to reference them:

```html
<link rel="stylesheet" href="{% theme_static 'css/theme.css' %}">
```

Why this matters:

- It ensures the correct theme is used
- It avoids hard-coding paths
- It allows themes to be moved or renamed safely

---

## Author Data and IndieWeb Conventions

Themes may display author information using template tags instead of hard-coded assumptions.

Example:

```html
<p>By {% author_hcard_name %}</p>
```

This ensures:

- Proper h-card resolution
- Graceful fallbacks
- Compatibility with IndieWeb features

Avoid:

- Hard-coding author names
- Assuming a single profile format

---

## Progressive Enhancement

Themes should work well even when:

- JavaScript is unavailable
- Optional features are disabled
- Content is missing or incomplete

Aim for:

- Semantic HTML
- CSS-first layouts
- Enhancements layered on top, not required

---

## Common Mistakes to Avoid

- Relying on undocumented context variables
- Assuming a setting always exists
- Hard-coding static paths
- Using themes to implement features

If you find yourself needing logic, stop and reconsider the design.

---

## Testing Your Theme

Before sharing or publishing a theme:

- Test with no settings changed
- Test with extreme setting values
- Test missing content
- Switch between themes to confirm isolation

A good theme fails quietly and predictably.

---

## Final Advice

A great theme:

- Looks good by default
- Is hard to break
- Makes few assumptions
- Respects user content

If you optimize for those goals, your theme will age well.
