from django import template

register = template.Library()


def _first_named_hcard(user):
    if not user:
        return None
    prefetched = getattr(user, "_prefetched_objects_cache", {}).get("hcards")
    if prefetched is not None:
        for hcard in prefetched:
            if hcard.name:
                return hcard
        return None
    return user.hcards.exclude(name="").order_by("pk").first()


@register.simple_tag(takes_context=True)
def author_hcard_name(context, user=None):
    """Return the author's h-card name with site-author fallback."""
    hcard = _first_named_hcard(user)
    if hcard:
        return hcard.name

    site_author_hcard = context.get("site_author_hcard")
    if site_author_hcard and site_author_hcard.name:
        return site_author_hcard.name

    settings = context.get("settings")
    if settings and settings.site_author_id:
        fallback_hcard = _first_named_hcard(settings.site_author)
        if fallback_hcard:
            return fallback_hcard.name

    return ""
