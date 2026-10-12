from __future__ import annotations

from django.template.loaders.cached import Loader as CachedLoader
from django.template.loaders.filesystem import Loader as FileSystemLoader

from core.themes import get_active_theme, get_active_theme_slug


class ThemeTemplateLoader(FileSystemLoader):
    """
    Look for template overrides in the active theme before falling back
    to the default loaders.
    """

    def get_dirs(self):
        theme = get_active_theme()
        if theme and theme.templates_path.exists():
            return [theme.templates_path]
        return []


class ThemeAwareCachedLoader(CachedLoader):
    """Cache compiled templates per active theme, including a preview override."""

    def cache_key(self, template_name, skip=None):
        return f"{get_active_theme_slug()}:{super().cache_key(template_name, skip)}"
