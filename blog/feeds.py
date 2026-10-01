from django.contrib.syndication.views import Feed
from django.urls import reverse
from django.urls import NoReverseMatch
from django.utils.feedgenerator import Rss201rev2Feed
from django.template.loader import render_to_string

from core.models import SiteConfiguration
from core.og import absolute_url
from core.themes import get_posts_index_url
from .models import Post
from .views import _interaction_payload


class PostsFeed(Feed):
    feed_type = Rss201rev2Feed

    def title(self):
        settings = SiteConfiguration.get_solo()
        return f"{settings.title} posts"

    def link(self):
        return get_posts_index_url()

    def description(self):
        settings = SiteConfiguration.get_solo()
        return settings.tagline

    def feed_url(self):
        if hasattr(self, "request"):
            try:
                return self.request.build_absolute_uri()
            except NoReverseMatch:
                return None
        return reverse("posts_feed")

    def get_object(self, request):
        self.request = request
        return None

    def items(self, obj=None):
        request = getattr(self, "request", None)

        selected_kinds = []
        selected_tags = []
        if request:
            for value in request.GET.getlist("kind"):
                selected_kinds.extend(
                    [chunk.strip().lower() for chunk in value.split(",") if chunk.strip()]
                )
            for value in request.GET.getlist("tag"):
                selected_tags.extend(
                    [chunk.strip().lower() for chunk in value.split(",") if chunk.strip()]
                )
        seen_kinds = set()
        selected_kinds = [kind for kind in selected_kinds if not (kind in seen_kinds or seen_kinds.add(kind))]
        seen_tags = set()
        selected_tags = [tag for tag in selected_tags if not (tag in seen_tags or seen_tags.add(tag))]
        valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
        selected_kinds = [kind for kind in selected_kinds if kind in valid_kinds]

        queryset = (
            Post.objects.live()
            .prefetch_related("attachments__asset")
            .order_by("-published_on")
        )
        if selected_kinds:
            queryset = queryset.filter(kind__in=selected_kinds)
        for tag in selected_tags:
            queryset = queryset.filter(tags__tag=tag)
        queryset = queryset.distinct()
        return queryset

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        request = getattr(self, "request", None)
        interaction = (
            _interaction_payload(item, request=request, fetch_remote=False)
            if item.kind in (Post.LIKE, Post.REPOST, Post.REPLY)
            else None
        )
        if interaction and request:
            target_url = interaction.get("target_url")
            if target_url:
                interaction["target_url"] = absolute_url(request, target_url)
            target = interaction.get("target")
            if target and target.get("original_url"):
                target["original_url"] = absolute_url(request, target["original_url"])
        media_items = []
        track_url = ""

        for attachment in item.attachments.all():
            asset = getattr(attachment, "asset", None)
            if not asset or not asset.file:
                continue
            url = absolute_url(request, asset.file.url) if request else asset.file.url
            if attachment.role == "gpx":
                track_url = url
                continue
            media_items.append(
                {
                    "kind": asset.kind,
                    "url": url,
                    "alt": asset.alt_text,
                    "caption": asset.caption,
                    "name": asset.file.name.rsplit("/", 1)[-1],
                }
            )

        return render_to_string(
            "blog/feed_item.html",
            {
                "post": item,
                "post_url": absolute_url(request, item.get_absolute_url()) if request else item.get_absolute_url(),
                "interaction": interaction,
                "media_items": media_items,
                "track_url": track_url,
            },
        )

    def item_link(self, item):
        return item.get_absolute_url()

    def item_pubdate(self, item):
        return item.published_on
