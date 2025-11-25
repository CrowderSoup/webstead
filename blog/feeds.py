from django.contrib.syndication.views import Feed
from django.urls import reverse
from django.urls import NoReverseMatch
from django.utils.feedgenerator import Rss201rev2Feed

from core.models import SiteConfiguration
from .models import Post


class PostsFeed(Feed):
    feed_type = Rss201rev2Feed

    def title(self):
        settings = SiteConfiguration.get_solo()
        return f"{settings.title} posts"

    def link(self):
        return reverse("posts")

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

        selected_kinds = request.GET.getlist("kind") if request else []
        valid_kinds = {kind for kind, _ in Post.KIND_CHOICES}
        selected_kinds = [kind for kind in selected_kinds if kind in valid_kinds]

        queryset = Post.objects.exclude(published_on__isnull=True).order_by("-published_on")
        if selected_kinds:
            queryset = queryset.filter(kind__in=selected_kinds)
        return queryset

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.html()

    def item_link(self, item):
        return item.get_absolute_url()

    def item_pubdate(self, item):
        return item.published_on
