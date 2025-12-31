from __future__ import annotations

import posixpath

from django import template
from django.templatetags.static import static

from core.themes import get_active_theme

register = template.Library()


@register.simple_tag(takes_context=True)
def theme_static(context, path):
    """Return a static URL for the active theme asset."""
    clean_path = (path or "").lstrip("/")
    prefix = ""

    try:
        active_theme = context["active_theme"]
    except Exception:
        active_theme = context.get("active_theme")
    if active_theme and getattr(active_theme, "static_prefix", None):
        prefix = active_theme.static_prefix
    else:
        try:
            theme_context = context["theme"]
        except Exception:
            theme_context = context.get("theme") or {}
        if isinstance(theme_context, dict):
            prefix = theme_context.get("static_prefix", "") or ""

    if not prefix:
        fallback_theme = get_active_theme()
        if fallback_theme:
            prefix = fallback_theme.static_prefix

    if prefix:
        prefix = prefix.rstrip("/")
        if clean_path.startswith(f"{prefix}/"):
            return static(clean_path)
        if clean_path:
            return static(posixpath.join(prefix, clean_path))
        return static(prefix)

    return static(clean_path)
