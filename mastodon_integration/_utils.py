"""Shared utility helpers for mastodon_integration (no model imports)."""


def _get(obj, key):
    """Safely get a key from either a dict or an AttribAccessDict/object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
