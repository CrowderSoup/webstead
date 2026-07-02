import logging

from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate

from core.theme_sync import reconcile_installed_themes
from core.themes import get_theme_static_dirs, sync_themes_from_storage


logger = logging.getLogger(__name__)
_startup_reconcile_ran = False
_startup_sync_ran = False


def _in_management_cmd() -> bool:
    """True when invoked via manage.py (not a long-lived server process)."""
    import sys
    return bool(sys.argv) and "manage.py" in sys.argv[0]


def _in_celery_worker() -> bool:
    """True when running as any Celery process (worker, beat, etc.)."""
    import sys
    return bool(sys.argv) and sys.argv[0].endswith("celery")


def _reset_startup_state() -> None:
    global _startup_reconcile_ran, _startup_sync_ran
    _startup_reconcile_ran = False
    _startup_sync_ran = False


def _run_startup_reconcile(*_args, **_kwargs) -> None:
    global _startup_reconcile_ran
    if _startup_reconcile_ran:
        return
    _startup_reconcile_ran = True
    try:
        from core.models import ThemeInstall

        results = reconcile_installed_themes()
        restored = [result.slug for result in results if result.restored]
        failures = [result.slug for result in results if result.status == ThemeInstall.STATUS_FAILED]

        if restored:
            logger.info("Reconciled %d theme(s) on startup: %s", len(restored), ", ".join(sorted(restored)))
        if failures:
            logger.warning("Theme reconciliation completed with failures for: %s", ", ".join(sorted(set(failures))))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Skipping theme reconciliation on startup: %s", exc)


def _run_startup_sync(*_args, **_kwargs) -> None:
    global _startup_sync_ran
    if _startup_sync_ran:
        return
    _startup_sync_ran = True
    try:
        slugs = sync_themes_from_storage(raise_errors=True)
        if slugs:
            logger.info("Synced %d theme(s) from storage on startup: %s", len(slugs), ", ".join(sorted(slugs)))
        else:
            logger.info("Theme storage reachable but no themes found to sync on startup.")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Skipping theme sync on startup: %s", exc)


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        reconcile_enabled = getattr(settings, "THEMES_STARTUP_RECONCILE", True)
        startup_sync_enabled = getattr(settings, "THEME_STARTUP_SYNC_ENABLED", True)

        if reconcile_enabled:
            post_migrate.connect(_run_startup_reconcile, dispatch_uid="core.startup_reconcile_migrate")
            if not _in_management_cmd() and not _in_celery_worker():
                try:
                    from core.tasks import reconcile_themes
                    reconcile_themes.apply_async(ignore_result=True)
                except Exception as exc:
                    logger.warning("Could not queue startup theme reconciliation: %s", exc)
        elif startup_sync_enabled:
            post_migrate.connect(_run_startup_sync, dispatch_uid="core.startup_sync_migrate")
            if not _in_management_cmd() and not _in_celery_worker():
                try:
                    from core.tasks import sync_themes
                    sync_themes.apply_async(ignore_result=True)
                except Exception as exc:
                    logger.warning("Could not queue startup theme sync: %s", exc)
        else:  # pragma: no cover - defensive
            logger.info("Theme startup sync disabled via THEME_STARTUP_SYNC_ENABLED.")

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
