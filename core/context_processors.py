import markdown

from django.urls import NoReverseMatch, reverse
from django.utils.safestring import mark_safe

from .models import SiteConfiguration

def site_configuration(request):
    settings = SiteConfiguration.get_solo()
    menu_items = None
    if settings.main_menu is not None:
        menu_items = settings.main_menu.menuitem_set.all()

    md = markdown.Markdown(extensions=["fenced_code"])
    settings.intro = mark_safe(md.convert(settings.intro))
    settings.bio = mark_safe(md.convert(settings.bio))

    feed_url = None
    try:
        feed_url = request.build_absolute_uri(reverse("posts_feed"))
    except NoReverseMatch:
        feed_url = None

    return {
        "settings": settings,
        "menu_items": menu_items,
        "feed_url": feed_url,
    }
