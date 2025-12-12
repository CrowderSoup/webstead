import logging

from django.apps import AppConfig
from django.conf import settings

from core.themes import get_theme_static_dirs, sync_themes_from_storage


logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        try:
            sync_themes_from_storage()
        except Exception as exc:  # pragma: no cover - defensive
            logger.info("Skipping theme sync on startup: %s", exc)

        try:
            static_dirs = list(getattr(settings, "STATICFILES_DIRS", []))
            existing_prefixes = {
                entry[0] for entry in static_dirs if isinstance(entry, (list, tuple)) and len(entry) == 2
            }
            for prefix, path in get_theme_static_dirs():
                if prefix not in existing_prefixes:
                    static_dirs.append((prefix, path))
            settings.STATICFILES_DIRS = static_dirs
        except Exception as exc:  # pragma: no cover - defensive
            logger.info("Could not refresh STATICFILES_DIRS for themes: %s", exc)
