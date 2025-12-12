from __future__ import annotations

from django.template.loaders.filesystem import Loader as FileSystemLoader

from core.themes import get_active_theme


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
