import markdown

from django.utils.safestring import mark_safe

from .models import SiteConfiguration

def site_configuration(request):
    settings = SiteConfiguration.get_solo()
    menu_items = settings.main_menu.menuitem_set.all()

    md = markdown.Markdown(extensions=["fenced_code"])
    settings.intro = mark_safe(md.convert(settings.intro))
    settings.bio = mark_safe(md.convert(settings.bio))

    return {
        "settings": settings,
        "menu_items": menu_items,
    }
