from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from typing import Iterable, Optional, Sequence

from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.utils import timezone
from django.utils.text import slugify

from .theme_validation import load_theme_metadata, validate_theme_dir

THEME_META_FILENAME = "theme.json"
THEMES_DIRNAME = "themes"
_DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


class ThemeUploadError(Exception):
    """Raised when a theme archive cannot be processed."""


@dataclass
class ThemeDefinition:
    slug: str
    path: Path
    label: str
    author: Optional[str] = None
    version: Optional[str] = None
    description: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def templates_path(self) -> Path:
        return self.path / "templates"

    @property
    def static_path(self) -> Path:
        return self.path / "static"

    @property
    def template_prefix(self) -> str:
        return f"{THEMES_DIRNAME}/{self.slug}/templates/"

    @property
    def static_prefix(self) -> str:
        return f"{THEMES_DIRNAME}/{self.slug}/static/"


def get_themes_root(base_dir: Optional[Path] = None) -> Path:
    """Return the absolute themes directory."""
    if base_dir is not None:
        return Path(base_dir)

    try:
        from django.conf import settings  # type: ignore

        configured_root = getattr(settings, "THEMES_ROOT", None)
        if configured_root:
            return Path(configured_root)
        return Path(getattr(settings, "BASE_DIR")) / THEMES_DIRNAME
    except Exception:
        # Settings may not be configured yet (e.g., during initial import in settings.py)
        return _DEFAULT_BASE_DIR / THEMES_DIRNAME


def get_theme_storage_prefix() -> str:
    try:
        from django.conf import settings  # type: ignore

        return getattr(settings, "THEME_STORAGE_PREFIX", THEMES_DIRNAME) or THEMES_DIRNAME
    except Exception:
        return THEMES_DIRNAME


def get_theme_storage() -> Storage:
    """Return the storage used for theme assets (defaults to the default storage)."""
    return default_storage


def _validate_safe_path(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_root == resolved_target:
        return resolved_target
    if resolved_root in resolved_target.parents:
        return resolved_target
    raise ThemeUploadError("Archive contained unsafe paths.")


def _iter_storage_files(storage: Storage, prefix: str) -> Iterable[str]:
    """Yield all file keys (recursively) under the given prefix."""
    dirs, files = storage.listdir(prefix)
    for filename in files:
        yield f"{prefix.rstrip('/')}/{filename}".lstrip("/")
    for directory in dirs:
        next_prefix = f"{prefix.rstrip('/')}/{directory}".rstrip("/") + "/"
        yield from _iter_storage_files(storage, next_prefix)


def _is_missing_storage_key_error(exc: Exception) -> bool:
    """
    Return True when an exception from storage represents a missing key/prefix.
    Handles S3 ClientError NoSuchKey responses which don't raise FileNotFoundError.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "NoSuchKey":
            return True
    return "NoSuchKey" in str(exc)


def download_theme_from_storage(slug: str, *, base_dir: Optional[Path] = None) -> bool:
    """Pull a theme from storage onto the local filesystem."""
    storage = get_theme_storage()
    prefix = f"{get_theme_storage_prefix().rstrip('/')}/{slug}/"
    theme_root = get_themes_root(base_dir) / slug
    found = False

    try:
        for key in _iter_storage_files(storage, prefix):
            found = True
            relative = key.removeprefix(prefix)
            destination = theme_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with storage.open(key, "rb") as source, destination.open("wb") as target_handle:
                    shutil.copyfileobj(source, target_handle)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to download theme file %s: %s", key, exc)
                continue
    except Exception as exc:
        logger.info("Unable to list theme %s in storage: %s", slug, exc)
        return False

    return found


def sync_themes_from_storage(base_dir: Optional[Path] = None, *, raise_errors: bool = False) -> list[str]:
    """
    Sync all remote theme directories into the local themes root.
    Returns the list of slugs that were found in storage.
    """
    storage = get_theme_storage()
    prefix = get_theme_storage_prefix().rstrip("/") + "/"
    downloaded: list[str] = []

    try:
        slugs, _files = storage.listdir(prefix)
    except Exception as exc:
        if raise_errors:
            raise
        logger.warning(f"Theme storage not reachable for {prefix}, skipping sync: {exc}")
        return downloaded

    for slug in slugs:
        if download_theme_from_storage(slug, base_dir=base_dir):
            downloaded.append(slug)

    return downloaded


def _write_theme_to_storage(slug: str, source_dir: Path) -> None:
    storage = get_theme_storage()
    prefix = f"{get_theme_storage_prefix().rstrip('/')}/{slug}"

    # Clear any existing keys under the prefix to avoid stale assets.
    try:
        for key in _iter_storage_files(storage, prefix + "/"):
            storage.delete(key)
    except Exception as exc:
        if not (isinstance(exc, FileNotFoundError) or _is_missing_storage_key_error(exc)):
            logger.warning("Unable to clear existing theme %s from storage: %s", slug, exc)

    for path in source_dir.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(source_dir).as_posix()
        storage_path = f"{prefix}/{relative}"
        with path.open("rb") as handle:
            storage.save(storage_path, File(handle))


def upload_theme_to_storage(slug: str, *, base_dir: Optional[Path] = None) -> None:
    """Persist a local theme directory to the configured storage backend."""
    theme_root = get_themes_root(base_dir) / slug
    if not theme_root.exists() or not (theme_root / THEME_META_FILENAME).exists():
        raise ThemeUploadError(f"Theme {slug} is not available on disk to upload.")
    _write_theme_to_storage(slug, theme_root)


def _write_theme_to_disk(slug: str, source_dir: Path, *, base_dir: Optional[Path] = None) -> Path:
    target_dir = get_themes_root(base_dir) / slug
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.rglob("*"):
        if item.is_dir():
            continue
        destination = target_dir / item.relative_to(source_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
    return target_dir


def theme_exists_on_disk(slug: str, *, base_dir: Optional[Path] = None) -> bool:
    """Return True when the theme directory and theme.json are present locally."""
    theme_root = get_themes_root(base_dir) / slug
    return theme_root.exists() and (theme_root / THEME_META_FILENAME).exists()


def theme_exists_in_storage(slug: str) -> bool:
    """Check whether a theme prefix exists in the configured storage backend."""
    storage = get_theme_storage()
    prefix = f"{get_theme_storage_prefix().rstrip('/')}/{slug}/"
    try:
        dirs, files = storage.listdir(prefix)
    except FileNotFoundError:
        return False
    except Exception as exc:
        if _is_missing_storage_key_error(exc):
            return False
        raise
    return bool(dirs or files)


def ensure_theme_on_disk(slug: str, *, base_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Ensure the given theme exists on disk by pulling it from storage if necessary.
    Returns the local path if available.
    """
    theme_root = get_themes_root(base_dir) / slug
    if theme_root.exists() and (theme_root / THEME_META_FILENAME).exists():
        return theme_root

    try:
        found = download_theme_from_storage(slug, base_dir=base_dir)
    except Exception as exc:
        logger.info("Unable to fetch theme %s from storage: %s", slug, exc)
        return None

    if found:
        return theme_root
    return None


def _find_theme_root(extracted_path: Path) -> Path:
    """Return the directory that contains theme.json, preferring nested roots."""
    top_level_meta = extracted_path / THEME_META_FILENAME
    if top_level_meta.exists():
        return extracted_path

    for child in extracted_path.iterdir():
        if child.is_dir() and (child / THEME_META_FILENAME).exists():
            return child

    raise ThemeUploadError("Archive must contain theme.json at the root of the theme.")


def _find_git_theme_root(clone_dir: Path, slug: str) -> Path:
    """Return the theme root for a cloned git repository."""
    top_level_meta = clone_dir / THEME_META_FILENAME
    if top_level_meta.exists():
        return clone_dir

    if slug:
        nested = clone_dir / THEMES_DIRNAME / slug
        if (nested / THEME_META_FILENAME).exists():
            return nested

    return _find_theme_root(clone_dir)


def _is_public_git_url(url: str) -> bool:
    parsed = urlsplit(url)
    if not parsed.scheme:
        if url.startswith(("/", "./", "../")):
            return True
        return "@" not in url
    if parsed.scheme in ("http", "https"):
        return parsed.username is None and parsed.password is None and "@" not in parsed.netloc
    if parsed.scheme == "file":
        return True
    return False


def _ensure_git_url_allowed(url: str) -> None:
    try:
        from django.conf import settings  # type: ignore

        allow_private = getattr(settings, "THEME_GIT_ALLOW_PRIVATE", False)
    except Exception:
        allow_private = False

    if allow_private:
        return

    if not _is_public_git_url(url):
        raise ThemeUploadError(
            "Private git URLs are disabled. Set THEME_GIT_ALLOW_PRIVATE to enable them."
        )


def _run_git(command: list[str], *, error_message: str) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise ThemeUploadError(f"{error_message}: {detail}") from exc
        raise ThemeUploadError(error_message) from exc


def _extract_theme_archive(uploaded_path: Path) -> tuple[str, Path, dict]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        destination = Path(tmp_dir)
        with zipfile.ZipFile(uploaded_path) as archive:
            for member in archive.namelist():
                _validate_safe_path(destination, destination / member)
            archive.extractall(destination)

        theme_root = _find_theme_root(destination)
        validation = validate_theme_dir(
            theme_root,
            meta_filename=THEME_META_FILENAME,
            require_directory_slug=theme_root != destination,
        )
        if not validation.is_valid:
            raise ThemeUploadError(validation.summary())
        metadata = validation.metadata

        slug_source = validation.slug or uploaded_path.stem
        slug = slugify(slug_source)
        if not slug:
            raise ThemeUploadError("Theme slug could not be determined from archive.")

        # Copy the extracted folder to a stable temp directory the caller can consume.
        final_tmp_dir = Path(tempfile.mkdtemp())
        shutil.copytree(theme_root, final_tmp_dir, dirs_exist_ok=True)
        return slug, final_tmp_dir, metadata


def ingest_theme_archive(uploaded_file, *, base_dir: Optional[Path] = None) -> ThemeDefinition:
    """
    Process an uploaded zip archive, validate it, and persist it to storage + disk.
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
        for chunk in uploaded_file.chunks():
            tmp_file.write(chunk)
        tmp_path = Path(tmp_file.name)

    slug: Optional[str] = None
    extracted_dir: Optional[Path] = None
    try:
        try:
            slug, extracted_dir, metadata = _extract_theme_archive(tmp_path)
        except zipfile.BadZipFile as exc:
            raise ThemeUploadError("Uploaded file must be a valid zip archive.") from exc
        except ThemeUploadError:
            raise
        except Exception as exc:
            raise ThemeUploadError(f"Unable to process archive: {exc}") from exc

        _write_theme_to_storage(slug, extracted_dir)
        local_path = _write_theme_to_disk(slug, extracted_dir, base_dir=base_dir)
        logger.info("Uploaded theme %s written to %s and storage", slug, local_path)
        try:
            from core.models import ThemeInstall

            ThemeInstall.objects.update_or_create(
                slug=slug,
                defaults={
                    "source_type": ThemeInstall.SOURCE_UPLOAD,
                    "source_url": "",
                    "source_ref": "",
                    "version": metadata.get("version") or "",
                    "checksum": "",
                    "last_synced_at": timezone.now(),
                    "last_sync_status": ThemeInstall.STATUS_SUCCESS,
                },
            )
        except Exception:
            logger.warning("Unable to persist theme install record for %s", slug, exc_info=True)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        if extracted_dir and extracted_dir.exists():
            shutil.rmtree(extracted_dir, ignore_errors=True)

    clear_template_caches()
    theme = get_theme(slug or "", base_dir=base_dir)
    if theme:
        return theme
    raise ThemeUploadError("Theme upload completed but could not be discovered locally.")


def install_theme_from_git(
    git_url: str,
    slug: str,
    *,
    ref: str = "",
    base_dir: Optional[Path] = None,
) -> ThemeDefinition:
    """
    Clone a theme from a git repository, validate, and persist to storage + disk.
    """
    if not git_url:
        raise ThemeUploadError("Git URL is required to install a theme.")

    normalized_slug = slugify(slug or "")
    if not normalized_slug:
        raise ThemeUploadError("Theme slug is required to install a theme.")

    _ensure_git_url_allowed(git_url)

    clone_dir = Path(tempfile.mkdtemp())
    try:
        _run_git(
            ["git", "clone", "--depth", "1", git_url, str(clone_dir)],
            error_message="Unable to clone theme repository",
        )
        if ref:
            _run_git(
                ["git", "-C", str(clone_dir), "fetch", "--depth", "1", "origin", ref],
                error_message=f"Unable to fetch ref '{ref}'",
            )
            _run_git(
                ["git", "-C", str(clone_dir), "checkout", ref],
                error_message=f"Unable to checkout ref '{ref}'",
            )

        theme_root = _find_git_theme_root(clone_dir, normalized_slug)
        validation = validate_theme_dir(
            theme_root,
            expected_slug=normalized_slug,
            meta_filename=THEME_META_FILENAME,
            require_directory_slug=theme_root != clone_dir,
        )
        if not validation.is_valid:
            raise ThemeUploadError(validation.summary(detailed=True))
        metadata = validation.metadata

        _write_theme_to_storage(normalized_slug, theme_root)
        local_path = _write_theme_to_disk(normalized_slug, theme_root, base_dir=base_dir)
        logger.info("Git theme %s written to %s and storage", normalized_slug, local_path)

        try:
            from core.models import ThemeInstall

            ThemeInstall.objects.update_or_create(
                slug=normalized_slug,
                defaults={
                    "source_type": ThemeInstall.SOURCE_GIT,
                    "source_url": git_url,
                    "source_ref": ref or "",
                    "version": metadata.get("version") or "",
                    "checksum": "",
                    "last_synced_at": timezone.now(),
                    "last_sync_status": ThemeInstall.STATUS_SUCCESS,
                },
            )
        except Exception:
            logger.warning(
                "Unable to persist theme install record for %s", normalized_slug, exc_info=True
            )

        clear_template_caches()
        theme = get_theme(normalized_slug, base_dir=base_dir)
        if theme:
            return theme
        raise ThemeUploadError("Theme install completed but could not be discovered locally.")
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def list_theme_files(slug: str, *, base_dir: Optional[Path] = None, suffixes: Optional[Sequence[str]] = None) -> list[str]:
    """
    Return a sorted list of file paths relative to the theme root.
    Optionally filter by allowed suffixes.
    """
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    if not theme_root.exists():
        return []

    normalized_suffixes = tuple(suffixes) if suffixes else None
    results: list[str] = []
    for path in theme_root.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(theme_root).as_posix()
        if normalized_suffixes and path.suffix not in normalized_suffixes:
            continue
        results.append(relative)

    results.sort()
    return results


def list_theme_directories(slug: str, *, base_dir: Optional[Path] = None) -> list[str]:
    """Return all directories (relative) under the theme, excluding the root."""
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    if not theme_root.exists():
        return []

    results: list[str] = []
    for path in theme_root.rglob("*"):
        if path.is_dir():
            relative = path.relative_to(theme_root).as_posix()
            if relative:
                results.append(relative)
    results.sort()
    return results


def read_theme_file(slug: str, relative_path: str, *, base_dir: Optional[Path] = None) -> str:
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    target = _validate_safe_path(theme_root, theme_root / relative_path)
    if not target.exists():
        raise ThemeUploadError(f"{relative_path} does not exist in {slug}.")
    try:
        return target.read_text()
    except Exception as exc:
        raise ThemeUploadError(f"Unable to read {relative_path}: {exc}") from exc


def save_theme_file(slug: str, relative_path: str, content: str, *, base_dir: Optional[Path] = None) -> Path:
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    target = _validate_safe_path(theme_root, theme_root / relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    storage_path = f"{get_theme_storage_prefix().rstrip('/')}/{slug}/{relative_path}"
    storage = get_theme_storage()
    try:
        storage.delete(storage_path)
    except Exception:
        pass
    storage.save(storage_path, File(target.open("rb")))

    clear_template_caches()
    return target


def create_theme_file(slug: str, relative_path: str, *, base_dir: Optional[Path] = None) -> Path:
    """
    Create an empty theme file at the given relative path.
    Raises ThemeUploadError if the path is outside the theme or already exists.
    """
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    target = _validate_safe_path(theme_root, theme_root / relative_path)
    if target.exists():
        raise ThemeUploadError(f"{relative_path} already exists.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")

    storage_path = f"{get_theme_storage_prefix().rstrip('/')}/{slug}/{relative_path}"
    storage = get_theme_storage()
    storage.save(storage_path, ContentFile(""))

    clear_template_caches()
    return target


def create_theme_folder(slug: str, relative_path: str, *, base_dir: Optional[Path] = None) -> Path:
    """
    Create a folder inside the given theme.
    Raises ThemeUploadError if the path is outside the theme or already exists.
    """
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    target = _validate_safe_path(theme_root, theme_root / relative_path)
    if target.exists():
        raise ThemeUploadError(f"{relative_path} already exists.")
    target.mkdir(parents=True, exist_ok=False)
    return target


def delete_theme_path(slug: str, relative_path: str, *, base_dir: Optional[Path] = None) -> None:
    """
    Delete a file or folder from the theme on disk and storage.
    """
    ensure_theme_on_disk(slug, base_dir=base_dir)
    theme_root = get_themes_root(base_dir) / slug
    target = _validate_safe_path(theme_root, theme_root / relative_path)
    if not target.exists():
        raise ThemeUploadError(f"{relative_path} does not exist.")

    was_dir = target.is_dir()
    if was_dir and any(target.iterdir()):
        raise ThemeUploadError("Only empty folders can be deleted.")

    if was_dir:
        shutil.rmtree(target)
    else:
        target.unlink()

    storage = get_theme_storage()
    storage_prefix = f"{get_theme_storage_prefix().rstrip('/')}/{slug}"
    storage_path = f"{storage_prefix}/{relative_path}".rstrip("/")

    try:
        if was_dir:
            prefix = storage_path.rstrip("/") + "/"
            for key in _iter_storage_files(storage, prefix):
                storage.delete(key)
        else:
            storage.delete(storage_path)
    except Exception:
        pass

    clear_template_caches()


def discover_themes(base_dir: Optional[Path] = None) -> list[ThemeDefinition]:
    """Inspect the themes directory and return discovered themes."""
    themes_root = get_themes_root(base_dir)
    themes_root.mkdir(parents=True, exist_ok=True)

    themes: list[ThemeDefinition] = []
    for theme_dir in themes_root.iterdir():
        if not theme_dir.is_dir():
            continue

        meta_path = theme_dir / THEME_META_FILENAME
        if not meta_path.exists():
            continue

        metadata, _errors = load_theme_metadata(meta_path)
        slug = theme_dir.name
        label = metadata.get("label") or slug.replace("-", " ").title()
        themes.append(
            ThemeDefinition(
                slug=slug,
                path=theme_dir,
                label=label,
                author=metadata.get("author"),
                version=metadata.get("version"),
                description=metadata.get("description"),
                metadata=metadata,
            )
        )

    themes.sort(key=lambda theme: theme.label.lower())
    return themes


def get_theme(slug: str, *, base_dir: Optional[Path] = None) -> Optional[ThemeDefinition]:
    for theme in discover_themes(base_dir=base_dir):
        if theme.slug == slug:
            return theme
    return None


def get_theme_static_dirs(base_dir: Optional[Path] = None, *, sync: bool = False) -> Iterable[tuple[str, Path]]:
    """Static directories for collectstatic to pick up with a stable prefix."""
    if sync:
        try:
            sync_themes_from_storage(base_dir=base_dir)
        except Exception as exc:
            logger.info("Skipping theme sync during static dir resolution: %s", exc)

    for theme in discover_themes(base_dir=base_dir):
        static_dir = theme.static_path
        if static_dir.exists():
            yield (f"{THEMES_DIRNAME}/{theme.slug}/static", static_dir)


def get_active_theme_slug() -> str:
    try:
        from core.models import SiteConfiguration

        return SiteConfiguration.get_solo().active_theme or ""
    except Exception:
        # Database might not be ready (migrations, checks), so return a safe default.
        return ""


def get_active_theme() -> Optional[ThemeDefinition]:
    slug = get_active_theme_slug()
    if not slug:
        return None
    return get_theme(slug)


def clear_template_caches() -> None:
    """Reset template caches so theme changes apply immediately."""
    try:
        from django.template import engines
    except Exception:
        return

    try:
        engine = engines["django"]
    except Exception:
        return

    try:
        engine.template_cache.clear()
    except Exception:
        pass

    for loader in getattr(engine, "template_loaders", []):
        reset = getattr(loader, "reset", None)
        if callable(reset):
            reset()
