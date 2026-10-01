from __future__ import annotations

import logging

import markdown
from django.template.loader import render_to_string

from core.plugins import BaseWidget

logger = logging.getLogger(__name__)


class TextWidget(BaseWidget):
    slug = "text"
    label = "Text / HTML Block"
    template_name = "widgets/text_widget.html"
    config_schema = {
        "fields": {
            "title": {"type": "string", "label": "Title"},
            "content": {"type": "text", "label": "Content (Markdown)"},
        }
    }

    def render(self, config: dict, request=None) -> str:
        md = markdown.Markdown(extensions=["fenced_code"])
        content_html = md.convert(config.get("content", ""))
        return render_to_string(
            self.template_name,
            {"title": config.get("title", ""), "content_html": content_html},
            request=request,
        )


class RecentPostsWidget(BaseWidget):
    slug = "recent_posts"
    label = "Recent Posts"
    template_name = "widgets/recent_posts_widget.html"
    config_schema = {
        "fields": {
            "title": {"type": "string", "label": "Title"},
            "count": {"type": "number", "label": "Count", "default": 5},
            "kind": {"type": "string", "label": "Post kind (optional)"},
        }
    }

    def render(self, config: dict, request=None) -> str:
        from blog.models import Post

        count = int(config.get("count") or 5)
        kind = config.get("kind", "")
        qs = Post.objects.live()
        if kind:
            qs = qs.filter(kind=kind)
        posts = qs.order_by("-published_on")[:count]
        return render_to_string(
            self.template_name,
            {"title": config.get("title", ""), "posts": posts},
            request=request,
        )


class ProfileWidget(BaseWidget):
    slug = "profile"
    label = "Profile"
    template_name = "widgets/profile_widget.html"
    config_schema = {
        "fields": {
            "show_photo": {"type": "boolean", "label": "Show photo", "default": True},
        }
    }

    def render(self, config: dict, request=None) -> str:
        from core.models import HCard, SiteConfiguration

        hcard = None
        try:
            site_config = SiteConfiguration.get_solo()
            if site_config.site_author_id:
                hcard = (
                    HCard.objects.filter(user_id=site_config.site_author_id)
                    .prefetch_related("photos")
                    .order_by("pk")
                    .first()
                )
        except Exception:
            logger.exception("ProfileWidget: could not fetch hcard")

        note_html = None
        if hcard and hcard.note:
            md = markdown.Markdown(extensions=["fenced_code"])
            from django.utils.safestring import mark_safe
            note_html = mark_safe(md.convert(hcard.note))

        show_photo = config.get("show_photo", True)
        return render_to_string(
            self.template_name,
            {"hcard": hcard, "show_photo": show_photo, "note_html": note_html},
            request=request,
        )
