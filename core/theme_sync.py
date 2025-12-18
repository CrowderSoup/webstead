from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

from core.theme_validation import validate_theme_dir
from core.themes import (
    THEME_META_FILENAME,
    ThemeUploadError,
    clear_template_caches,
    download_theme_from_storage,
    theme_exists_in_storage,
    theme_exists_on_disk,
    upload_theme_to_storage,
    _find_theme_root,  # type: ignore
    _write_theme_to_disk,  # type: ignore
    _write_theme_to_storage,  # type: ignore
)

if TYPE_CHECKING:
    from core.models import ThemeInstall

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    slug: str
    status: str
    detail: str = ""
    restored: bool = False


def rehydrate_theme_from_git(install: ThemeInstall, *, base_dir: Optional[Path] = None) -> bool:
    """Clone a git-backed theme and write it to disk + storage."""
    if not install.source_url:
        raise ThemeUploadError(f"Theme {install.slug} missing source_url for git restore.")

    clone_dir = Path(tempfile.mkdtemp())
    try:
        command = ["git", "clone", "--depth", "1", install.source_url, str(clone_dir)]
        if install.source_ref:
            command = ["git", "clone", "--depth", "1", "--branch", install.source_ref, install.source_url, str(clone_dir)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        theme_root = _find_theme_root(clone_dir)
        validation = validate_theme_dir(theme_root, expected_slug=install.slug, meta_filename=THEME_META_FILENAME)
        if not validation.is_valid:
            raise ThemeUploadError(validation.summary())

        _write_theme_to_storage(install.slug, theme_root)
        _write_theme_to_disk(install.slug, theme_root, base_dir=base_dir)
        return True
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _mark_status(install: ThemeInstall, status: str) -> None:
    install.last_synced_at = timezone.now()
    install.last_sync_status = status
    install.save(update_fields=["last_synced_at", "last_sync_status"])


def _reconcile_install(
    install: ThemeInstall, *, base_dir: Optional[Path] = None, upload_missing_to_storage: bool = False
) -> ReconcileResult:
    from core.models import ThemeInstall

    slug = install.slug
    storage_available = True
    storage_exists = False
    storage_error: Optional[Exception] = None

    try:
        storage_exists = theme_exists_in_storage(slug)
    except Exception as exc:
        storage_available = False
        storage_error = exc

    local_exists = theme_exists_on_disk(slug, base_dir=base_dir)
    if not local_exists:
        if install.source_type == ThemeInstall.SOURCE_GIT:
            try:
                restored = rehydrate_theme_from_git(install, base_dir=base_dir)
            except Exception as exc:
                logger.warning("Failed to restore git theme %s: %s", slug, exc)
                _mark_status(install, ThemeInstall.STATUS_FAILED)
                return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "git restore failed")

            if restored:
                _mark_status(install, ThemeInstall.STATUS_SUCCESS)
                return ReconcileResult(slug, ThemeInstall.STATUS_SUCCESS, "restored from git", restored=True)

        if storage_available and storage_exists:
            try:
                restored = download_theme_from_storage(slug, base_dir=base_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Unable to download missing theme %s from storage: %s", slug, exc)
                _mark_status(install, ThemeInstall.STATUS_FAILED)
                return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "storage restore failed")

            if restored:
                _mark_status(install, ThemeInstall.STATUS_SUCCESS)
                return ReconcileResult(slug, ThemeInstall.STATUS_SUCCESS, "restored from storage", restored=True)

        if not storage_available:
            logger.warning("Theme %s missing locally and storage unavailable: %s", slug, storage_error)
            _mark_status(install, ThemeInstall.STATUS_FAILED)
            return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "storage unavailable")

        logger.warning("Theme %s missing locally and no source available to restore.", slug)
        _mark_status(install, ThemeInstall.STATUS_FAILED)
        return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "missing locally")

    if storage_available:
        if not storage_exists:
            if upload_missing_to_storage:
                try:
                    upload_theme_to_storage(slug, base_dir=base_dir)
                    _mark_status(install, ThemeInstall.STATUS_SUCCESS)
                    return ReconcileResult(slug, ThemeInstall.STATUS_SUCCESS, "uploaded to storage")
                except Exception as exc:
                    logger.warning("Unable to upload theme %s to storage: %s", slug, exc)
                    _mark_status(install, ThemeInstall.STATUS_FAILED)
                    return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "upload to storage failed")

            logger.warning("Theme %s missing from storage; upload disabled.", slug)
            _mark_status(install, ThemeInstall.STATUS_FAILED)
            return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "missing in storage")
    else:
        logger.warning("Storage unavailable while reconciling theme %s: %s", slug, storage_error)
        _mark_status(install, ThemeInstall.STATUS_FAILED)
        return ReconcileResult(slug, ThemeInstall.STATUS_FAILED, "storage unavailable")

    _mark_status(install, ThemeInstall.STATUS_SUCCESS)
    return ReconcileResult(slug, ThemeInstall.STATUS_SUCCESS, "already in sync")


def reconcile_installed_themes(
    *, base_dir: Optional[Path] = None, upload_missing_to_storage: Optional[bool] = None
) -> list[ReconcileResult]:
    """
    Use installed theme records as the source of truth and reconcile local + storage copies.
    """
    from core.models import ThemeInstall

    upload_missing = (
        getattr(settings, "THEMES_STARTUP_UPLOAD_MISSING", False)
        if upload_missing_to_storage is None
        else upload_missing_to_storage
    )

    results: list[ReconcileResult] = []
    restored_any = False

    for install in ThemeInstall.objects.all():
        result = _reconcile_install(
            install,
            base_dir=base_dir,
            upload_missing_to_storage=upload_missing,
        )
        restored_any = restored_any or result.restored
        results.append(result)

    if restored_any:
        clear_template_caches()

    return results
