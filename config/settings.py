from pathlib import Path
import sys

import environ
from core.themes import get_theme_static_dirs

# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
RUNNING_TESTS = "test" in sys.argv

env = environ.Env(
    DEBUG=(bool, False),
)

environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env.bool("DEBUG", default=False)
SECRET_KEY = env("SECRET_KEY")

# Themes
THEMES_ROOT = env("THEMES_ROOT", default=str(BASE_DIR / "themes"))
THEME_STORAGE_PREFIX = env("THEME_STORAGE_PREFIX", default="themes")
THEME_STARTUP_SYNC_ENABLED = env.bool("THEME_STARTUP_SYNC_ENABLED", default=True)
THEMES_STARTUP_RECONCILE = env.bool("THEMES_STARTUP_RECONCILE", default=True)
THEMES_STARTUP_UPLOAD_MISSING = env.bool("THEMES_STARTUP_UPLOAD_MISSING", default=False)

# ---------------------------------------------------------------------------
# Hosts and security
# ---------------------------------------------------------------------------

ALLOWED_HOSTS: list[str] = []
INTERNAL_IPS = ["127.0.0.1"]
CSRF_TRUSTED_ORIGINS: list[str] = []

if not DEBUG:
    # Example: ALLOWED_HOSTS=example.com,.example.org
    ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
    CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    # Local apps
    "core.apps.CoreConfig",
    "blog.apps.BlogConfig",
    "files.apps.FilesConfig",
    "micropub.apps.MicropubConfig",
    "analytics.apps.AnalyticsConfig",

    # Django admin UI
    "unfold",

    # Django apps
    "config.apps.CustomAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party apps
    "solo",
    "storages",
]

if DEBUG:
    INSTALLED_APPS.append("debug_toolbar")

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "core.middleware.RedirectMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "analytics.middleware.AnalyticsMiddleware",
]

if DEBUG:
    MIDDLEWARE.append("debug_toolbar.middleware.DebugToolbarMiddleware")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {
            "loaders": [
                "core.template_loaders.ThemeTemplateLoader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_configuration",
                "core.context_processors.theme",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

if RUNNING_TESTS:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    _DB_ENGINE = env("DB_ENGINE", default="django.db.backends.postgresql")

    if _DB_ENGINE == "django.db.backends.sqlite3":
        DATABASES = {
            "default": {
                "ENGINE": _DB_ENGINE,
                "NAME": env("DB_NAME", default=str(BASE_DIR / "db.sqlite3")),
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": _DB_ENGINE,
                "NAME": env("DB_NAME"),
                "USER": env("DB_USER"),
                "PASSWORD": env("DB_PASS"),
                "HOST": env("DB_HOST"),
                "PORT": env("DB_PORT"),
            }
        }

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Storage and files
# ---------------------------------------------------------------------------

if RUNNING_TESTS:
    MEDIA_URL = "/media/"
    MEDIA_ROOT = BASE_DIR / "test_media"
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": MEDIA_ROOT},
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
else:
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME")

    AWS_QUERYSTRING_AUTH = False
    AWS_DEFAULT_ACL = None
    AWS_S3_USE_SSL = False
    AWS_S3_VERIFY = False
    AWS_S3_ADDRESSING_STYLE = "path"
    AWS_DEFAULT_ACL = "public-read"
    AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "max-age=86400"}
    AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default=None)

    _S3_OPTIONS = {
        "access_key": AWS_ACCESS_KEY_ID,
        "secret_key": AWS_SECRET_ACCESS_KEY,
    }

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": _S3_OPTIONS,
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": _S3_OPTIONS,
        },
    }

    if not DEBUG:
        AWS_S3_VERIFY = True

    MEDIA_URL = f"{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/"

# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static", *get_theme_static_dirs(BASE_DIR)]

WHITENOISE_MAX_AGE = 31536000

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
