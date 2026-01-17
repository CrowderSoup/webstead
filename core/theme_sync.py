from __future__ import annotations

import logging
import shutil
import tempfile
import time
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
    _find_git_theme_root,  # type: ignore
    _run_git,  # type: ignore
    _run_git_capture,  # type: ignore
    theme_exists_in_storage,
    theme_exists_on_disk,
    upload_theme_to_storage,
    _write_theme_to_disk,  # type: ignore
    _write_theme_to_storage,  # type: ignore
)
from core.observability import duration_ms, log_theme_operation, truncate_error

if TYPE_CHECKING:
    from core.models import ThemeInstall

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    slug: str
    status: str
    action: str
    detail: str = ""
    restored: bool = False
    source_type: str = ""
    ref: str = ""
    duration_ms: int = 0


def rehydrate_theme_from_git(install: ThemeInstall, *, base_dir: Optional[Path] = None) -> bool:
    """Clone a git-backed theme and write it to disk + storage."""
    if not install.source_url:
        raise ThemeUploadError(f"Theme {install.slug} missing source_url for git restore.")

    clone_dir = Path(tempfile.mkdtemp())
    try:
        command = ["git", "clone", "--depth", "1", install.source_url, str(clone_dir)]
        if install.source_ref:
            command = [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                install.source_ref,
                install.source_url,
                str(clone_dir),
            ]
        _run_git(command, error_message="Unable to clone theme repository")

        commit = _run_git_capture(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            error_message="Unable to determine theme commit",
        )

        theme_root = _find_git_theme_root(clone_dir, install.slug)
        validation = validate_theme_dir(theme_root, expected_slug=install.slug, meta_filename=THEME_META_FILENAME)
        if not validation.is_valid:
            raise ThemeUploadError(validation.summary())

        _write_theme_to_storage(install.slug, theme_root)
        _write_theme_to_disk(install.slug, theme_root, base_dir=base_dir)
        if commit:
            install.last_synced_commit = commit
            install.save(update_fields=["last_synced_commit"])
        return True
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _mark_status(
    install: ThemeInstall,
    status: str,
    *,
    error: str = "",
    dry_run: bool = False,
) -> None:
    from core.models import ThemeInstall

    if dry_run:
        return
    install.last_synced_at = timezone.now()
    install.last_sync_status = status
    update_fields = ["last_synced_at", "last_sync_status", "last_sync_error"]
    if status == ThemeInstall.STATUS_FAILED:
        install.last_sync_error = truncate_error(error)
    else:
        install.last_sync_error = ""
    install.save(update_fields=update_fields)


def _reconcile_install(
    install: ThemeInstall,
    *,
    base_dir: Optional[Path] = None,
    upload_missing_to_storage: bool = False,
    dry_run: bool = False,
) -> ReconcileResult:
    from core.models import ThemeInstall

    slug = install.slug
    started_at = time.monotonic()
    storage_available = True
    storage_exists = False
    storage_error: Optional[Exception] = None

    def _result(status: str, action: str, detail: str, *, restored: bool = False) -> ReconcileResult:
        return ReconcileResult(
            slug=slug,
            status=status,
            action=action,
            detail=detail,
            restored=restored,
            source_type=install.source_type,
            ref=install.source_ref or "",
            duration_ms=duration_ms(started_at),
        )

    try:
        storage_exists = theme_exists_in_storage(slug)
    except Exception as exc:
        storage_available = False
        storage_error = exc

    local_exists = theme_exists_on_disk(slug, base_dir=base_dir)
    if not local_exists:
        if install.source_type == ThemeInstall.SOURCE_GIT:
            if dry_run:
                if not install.source_url:
                    _mark_status(
                        install,
                        ThemeInstall.STATUS_FAILED,
                        error="missing git source_url",
                        dry_run=dry_run,
                    )
                    return _result(ThemeInstall.STATUS_FAILED, "failed", "missing git source_url")
                return _result(
                    ThemeInstall.STATUS_SUCCESS,
                    "downloaded",
                    "would restore from git",
                    restored=True,
                )
            try:
                restored = rehydrate_theme_from_git(install, base_dir=base_dir)
            except Exception as exc:
                logger.warning("Failed to restore git theme %s: %s", slug, exc)
                _mark_status(install, ThemeInstall.STATUS_FAILED, error=str(exc), dry_run=dry_run)
                return _result(ThemeInstall.STATUS_FAILED, "failed", "git restore failed")

            if restored:
                _mark_status(install, ThemeInstall.STATUS_SUCCESS, dry_run=dry_run)
                return _result(
                    ThemeInstall.STATUS_SUCCESS,
                    "downloaded",
                    "restored from git",
                    restored=True,
                )

        if storage_available and storage_exists:
            if dry_run:
                return _result(
                    ThemeInstall.STATUS_SUCCESS,
                    "downloaded",
                    "would restore from storage",
                    restored=True,
                )
            try:
                restored = download_theme_from_storage(slug, base_dir=base_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Unable to download missing theme %s from storage: %s", slug, exc)
                _mark_status(install, ThemeInstall.STATUS_FAILED, error=str(exc), dry_run=dry_run)
                return _result(ThemeInstall.STATUS_FAILED, "failed", "storage restore failed")

            if restored:
                _mark_status(install, ThemeInstall.STATUS_SUCCESS, dry_run=dry_run)
                return _result(
                    ThemeInstall.STATUS_SUCCESS,
                    "downloaded",
                    "restored from storage",
                    restored=True,
                )

        if not storage_available:
            logger.warning("Theme %s missing locally and storage unavailable: %s", slug, storage_error)
            _mark_status(install, ThemeInstall.STATUS_FAILED, error="storage unavailable", dry_run=dry_run)
            return _result(ThemeInstall.STATUS_FAILED, "failed", "storage unavailable")

        logger.warning("Theme %s missing locally and no source available to restore.", slug)
        _mark_status(install, ThemeInstall.STATUS_FAILED, error="missing locally", dry_run=dry_run)
        return _result(ThemeInstall.STATUS_FAILED, "failed", "missing locally")

    if storage_available:
        if not storage_exists:
            if upload_missing_to_storage:
                if dry_run:
                    return _result(
                        ThemeInstall.STATUS_SUCCESS,
                        "uploaded",
                        "would upload to storage",
                    )
                try:
                    upload_theme_to_storage(slug, base_dir=base_dir)
                    _mark_status(install, ThemeInstall.STATUS_SUCCESS, dry_run=dry_run)
                    return _result(ThemeInstall.STATUS_SUCCESS, "uploaded", "uploaded to storage")
                except Exception as exc:
                    logger.warning("Unable to upload theme %s to storage: %s", slug, exc)
                    _mark_status(install, ThemeInstall.STATUS_FAILED, error=str(exc), dry_run=dry_run)
                    return _result(ThemeInstall.STATUS_FAILED, "failed", "upload to storage failed")

            logger.warning("Theme %s missing from storage; upload disabled.", slug)
            _mark_status(install, ThemeInstall.STATUS_FAILED, error="missing in storage", dry_run=dry_run)
            return _result(ThemeInstall.STATUS_FAILED, "failed", "missing in storage")
    else:
        logger.warning("Storage unavailable while reconciling theme %s: %s", slug, storage_error)
        _mark_status(install, ThemeInstall.STATUS_FAILED, error="storage unavailable", dry_run=dry_run)
        return _result(ThemeInstall.STATUS_FAILED, "failed", "storage unavailable")

    _mark_status(install, ThemeInstall.STATUS_SUCCESS, dry_run=dry_run)
    return _result(ThemeInstall.STATUS_SUCCESS, "skipped", "already in sync")


def reconcile_installed_themes(
    *,
    base_dir: Optional[Path] = None,
    upload_missing_to_storage: Optional[bool] = None,
    dry_run: bool = False,
    installs: Optional[list[ThemeInstall]] = None,
    slugs: Optional[list[str]] = None,
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

    if installs is None:
        installs_query = ThemeInstall.objects.all()
        if slugs:
            installs_query = installs_query.filter(slug__in=slugs)
        installs = list(installs_query.order_by("slug"))

    for install in installs:
        result = _reconcile_install(
            install,
            base_dir=base_dir,
            upload_missing_to_storage=upload_missing,
            dry_run=dry_run,
        )
        _log_reconcile_result(result, dry_run=dry_run)
        if not dry_run:
            restored_any = restored_any or result.restored
        results.append(result)

    if restored_any and not dry_run:
        clear_template_caches()

    return results


def _log_reconcile_result(result: ReconcileResult, *, dry_run: bool = False) -> None:
    log_theme_operation(
        logger,
        theme_slug=result.slug,
        operation="reconcile",
        source_type=result.source_type,
        ref=result.ref,
        status=result.status,
        duration_ms_value=result.duration_ms,
        detail=result.detail,
        dry_run=dry_run,
    )
