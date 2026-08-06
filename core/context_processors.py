import markdown

from django.core.cache import cache
from django.core.exceptions import DisallowedHost
from django.urls import NoReverseMatch, reverse
from django.utils.safestring import mark_safe
from django.db.models import Q

from .models import HCard, SiteConfiguration
from .og import default_image_url
from .themes import get_active_theme, resolve_theme_settings, get_active_theme_settings, get_posts_index_url, get_active_theme_widget_areas
from blog.models import Comment
from micropub.models import Webmention

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
        site_author_hcard = (
            settings.site_author.hcards.prefetch_related("photos", "urls")
            .order_by("pk")
            .first()
        )

    if site_author_hcard and site_author_hcard.name:
        site_author_display_name = site_author_hcard.name

    if site_author_hcard:
        md = markdown.Markdown(extensions=["fenced_code"])
        site_author_hcard.note_html = mark_safe(md.convert(site_author_hcard.note or ""))

    feed_url = None
    try:
        feed_url = request.build_absolute_uri(reverse("posts_feed"))
    except (NoReverseMatch, DisallowedHost):
        feed_url = None

    theme_settings = get_active_theme_settings()
    home_feed_mode = theme_settings.get("home_feed_mode", "blog")
    posts_index_url = get_posts_index_url()

    try:
        og_default_image = default_image_url(request, settings=settings, site_author_hcard=site_author_hcard)
    except DisallowedHost:
        og_default_image = None

    try:
        request_url = request.build_absolute_uri()
    except DisallowedHost:
        request_url = None

    return {
        "settings": settings,
        "menu_items": menu_items,
        "footer_menu_items": footer_menu_items,
        "feed_url": feed_url,
        "home_feed_mode": home_feed_mode,
        "posts_index_url": posts_index_url,
        "site_author_hcard": site_author_hcard,
        "site_author_display_name": site_author_display_name,
        "og_default_image": og_default_image,
        "request_url": request_url,
    }


def theme(request):
    active_theme = get_active_theme()
    settings_obj = SiteConfiguration.get_solo()
    theme_settings = {}
    theme_settings_schema = {}
    if active_theme:
        theme_settings_schema = active_theme.settings_schema
        stored_settings = (
            settings_obj.theme_settings.get(active_theme.slug, {})
            if isinstance(settings_obj.theme_settings, dict)
            else {}
        )
        theme_settings = resolve_theme_settings(theme_settings_schema, stored_settings)
    return {
        "active_theme": active_theme,
        "theme": {
            "slug": active_theme.slug if active_theme else "",
            "label": active_theme.label if active_theme else "Default",
            "metadata": active_theme.metadata if active_theme else {},
            "settings": theme_settings,
            "settings_schema": theme_settings_schema,
            "template_prefix": active_theme.template_prefix if active_theme else "",
            "static_prefix": active_theme.static_prefix if active_theme else "",
            "widget_areas": active_theme.widget_areas if active_theme else [],
        },
    }


def interactions_counts(request):
    if not request.path.startswith("/admin/"):
        return {}

    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_staff:
        return {
            "interactions_pending_count": 0,
            "admin_profile_photo_url": "",
            "admin_profile_display_name": "",
            "admin_profile_initials": "",
        }

    cache_key = f"interactions_counts_{user.pk}"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    host = request.get_host()
    if not host:
        return {
            "interactions_pending_count": 0,
            "admin_profile_photo_url": "",
            "admin_profile_display_name": user.get_username(),
            "admin_profile_initials": (user.get_username() or "U")[:1].upper(),
        }
    prefixes = [f"http://{host}", f"https://{host}"]
    target_query = Q()
    for prefix in prefixes:
        target_query |= Q(target__startswith=prefix)

    hcard = HCard.objects.filter(user=user).order_by("pk").first()
    display_name = ""
    if hcard and hcard.name:
        display_name = hcard.name
    elif user.get_full_name():
        display_name = user.get_full_name()
    else:
        display_name = user.get_username()

    initials_source = display_name.strip() or "U"
    parts = initials_source.split()
    if len(parts) >= 2:
        initials = f"{parts[0][0]}{parts[1][0]}".upper()
    else:
        initials = initials_source[:1].upper()

    pending_comments = Comment.objects.filter(status=Comment.PENDING).count()
    pending_webmentions = (
        Webmention.objects.filter(status=Webmention.PENDING)
        .filter(target_query)
        .count()
    )
    result = {
        "interactions_pending_count": pending_comments + pending_webmentions,
        "admin_profile_photo_url": hcard.primary_photo_url if hcard else "",
        "admin_profile_display_name": display_name,
        "admin_profile_initials": initials,
    }
    cache.set(cache_key, result, 30)
    return result
