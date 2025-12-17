import markdown

from django.urls import NoReverseMatch, reverse
from django.utils.safestring import mark_safe

from .models import SiteConfiguration
from .themes import get_active_theme

def site_configuration(request):
    settings = SiteConfiguration.get_solo()
    menu_items = None
    footer_menu_items = None
    if settings.main_menu is not None:
        menu_items = settings.main_menu.menuitem_set.all()
    if settings.footer_menu is not None:
        footer_menu_items = settings.footer_menu.menuitem_set.all()

    site_author_hcard = None
    site_author_display_name = ""
    if settings.site_author_id:
        site_author_display_name = (
            settings.site_author.get_full_name()
            or settings.site_author.get_username()
            or ""
        )
        site_author_hcard = settings.site_author.hcards.order_by("pk").first()

    if site_author_hcard and site_author_hcard.name:
        site_author_display_name = site_author_hcard.name

    if site_author_hcard:
        md = markdown.Markdown(extensions=["fenced_code"])
        site_author_hcard.note_html = mark_safe(md.convert(site_author_hcard.note or ""))

    feed_url = None
    try:
        feed_url = request.build_absolute_uri(reverse("posts_feed"))
    except NoReverseMatch:
        feed_url = None

    return {
        "settings": settings,
        "menu_items": menu_items,
        "footer_menu_items": footer_menu_items,
        "feed_url": feed_url,
        "site_author_hcard": site_author_hcard,
        "site_author_display_name": site_author_display_name,
    }


def theme(request):
    active_theme = get_active_theme()
    return {
        "active_theme": active_theme,
        "theme": {
            "slug": active_theme.slug if active_theme else "",
            "label": active_theme.label if active_theme else "Default",
            "metadata": active_theme.metadata if active_theme else {},
            "template_prefix": active_theme.template_prefix if active_theme else "",
            "static_prefix": active_theme.static_prefix if active_theme else "",
        },
    }
