from __future__ import annotations

from typing import Any

from .models import Post


def activity_from_mf2(post: Post) -> dict[str, str]:
    activity = {"name": "", "track_url": ""}
    mf2_data: dict[str, Any] = post.mf2 if isinstance(post.mf2, dict) else {}
    activity_items = mf2_data.get("activity") or []
    if isinstance(activity_items, list):
        activity_item = activity_items[0] if activity_items else {}
    else:
        activity_item = activity_items
    if isinstance(activity_item, dict):
        properties = activity_item.get("properties") or {}
        if isinstance(properties, dict):
            for key in ("name", "activity-type", "category"):
                values = properties.get(key) or []
                if values and not activity["name"]:
                    activity["name"] = str(values[0])
            track_values = properties.get("track") or []
            if track_values:
                activity["track_url"] = track_values[0]
    if not activity["track_url"] and post.gpx_attachment:
        activity["track_url"] = post.gpx_attachment.asset.file.url
    return activity
