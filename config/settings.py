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
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")
if not FIELD_ENCRYPTION_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEY must be set to a non-empty Fernet key. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

# Themes
THEMES_ROOT = env("THEMES_ROOT", default=str(BASE_DIR / "themes"))
THEME_STORAGE_PREFIX = env("THEME_STORAGE_PREFIX", default="themes")
THEME_STARTUP_SYNC_ENABLED = env.bool("THEME_STARTUP_SYNC_ENABLED", default=True)
THEMES_STARTUP_RECONCILE = env.bool("THEMES_STARTUP_RECONCILE", default=True)
THEMES_STARTUP_UPLOAD_MISSING = env.bool("THEMES_STARTUP_UPLOAD_MISSING", default=False)

# Plugins
PLUGINS_ROOT = env("PLUGINS_ROOT", default=str(BASE_DIR / "plugins"))

# Add plugins directory to sys.path so plugin packages are importable
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

# Load third-party plugin apps
try:
    from config.installed_plugins import INSTALLED_PLUGIN_APPS
except ImportError:
    INSTALLED_PLUGIN_APPS = []

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

WEBMENTION_TRUSTED_DOMAINS = env.list("WEBMENTION_TRUSTED_DOMAINS", default=[])
MICROSUB_BASE_URL = env("MICROSUB_BASE_URL", default="")

# Comments + spam protection
AKISMET_API_KEY = env("AKISMET_API_KEY", default="")
TURNSTILE_SITE_KEY = env("TURNSTILE_SITE_KEY", default="")
TURNSTILE_SECRET_KEY = env("TURNSTILE_SECRET_KEY", default="")

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
    "indieauth.apps.IndieauthConfig",
    "analytics.apps.AnalyticsConfig",
    "site_admin.apps.SiteAdminConfig",
    "widgets.apps.WidgetsConfig",
    "microsub.apps.MicrosubConfig",
    "mastodon_integration.apps.MastodonIntegrationConfig",

    # Third-party plugins from config/installed_plugins.py
    *INSTALLED_PLUGIN_APPS,

    # Django apps
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party apps
    "solo",
    "storages",
    "django_celery_beat",
    "encrypted_model_fields",
]

if DEBUG:
    INSTALLED_APPS.append("debug_toolbar")

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "analytics.middleware.AnalyticsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "core.middleware.RedirectMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "site_admin.middleware.SiteAdminHtmxMessagesMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
            "loaders": (
                [
                    "core.template_loaders.ThemeTemplateLoader",
                    "django.template.loaders.filesystem.Loader",
                    "django.template.loaders.app_directories.Loader",
                ]
                if DEBUG or RUNNING_TESTS
                else [
                    (
                        "django.template.loaders.cached.Loader",
                        [
                            "core.template_loaders.ThemeTemplateLoader",
                            "django.template.loaders.filesystem.Loader",
                            "django.template.loaders.app_directories.Loader",
                        ],
                    )
                ]
            ),
            "builtins": [
                "core.templatetags.theme",
                "site_admin.templatetags.site_admin_tags",
            ],
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_configuration",
                "core.context_processors.interactions_counts",
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
# Cache
# ---------------------------------------------------------------------------

if RUNNING_TESTS:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": env("CACHE_URL", default="redis://localhost:6379/1"),
            "OPTIONS": {
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
            },
        }
    }

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_DEFAULT_QUEUE = env("CELERY_TASK_DEFAULT_QUEUE", default="webstead")
CELERY_WORKER_POOL = env("CELERY_WORKER_POOL", default="prefork")
CELERY_WORKER_CONCURRENCY = env.int("CELERY_WORKER_CONCURRENCY", default=None)
CELERY_TASK_IGNORE_RESULT = True

if RUNNING_TESTS:
    # These use the CELERY_ namespace prefix, which config/celery.py strips and
    # lowercases via app.config_from_object(namespace="CELERY"), mapping them to
    # the Celery 5 lowercase keys task_always_eager / task_eager_propagates.
    # If that namespace argument is ever removed, rename to the lowercase forms.
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

CELERY_BEAT_SCHEDULE = {
    "poll-microsub-feeds": {
        "task": "microsub.tasks.poll_microsub_feeds",
        "schedule": 900,  # every 15 min (matches REFETCH_INTERVAL_SECONDS)
    },
    "poll-mastodon-timeline": {
        "task": "mastodon_integration.tasks.poll_mastodon_timeline",
        "schedule": 900,  # every 15 min
    },
    "poll-mastodon-notifications": {
        "task": "mastodon_integration.tasks.poll_mastodon_notifications",
        "schedule": 900,  # every 15 min
    },
}

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

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "micropub.webmention": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "site_admin.views": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

if RUNNING_TESTS:
    LOGGING["loggers"].update(
        {
            "core.themes": {"handlers": [], "level": "ERROR", "propagate": False},
            "core.theme_sync": {"handlers": [], "level": "ERROR", "propagate": False},
            # Suppress 5xx request logs (e.g. intentional 502 in preview tests)
            "django.request": {"handlers": [], "level": "CRITICAL", "propagate": False},
            # Suppress expected warning logs from websub/subscribe tests
            "microsub.views": {"handlers": [], "level": "CRITICAL", "propagate": False},
        }
    )
