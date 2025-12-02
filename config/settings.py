from pathlib import Path

import environ

# ---------------------------------------------------------------------------
# Paths and environment
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)

environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env.bool("DEBUG", default=False)
SECRET_KEY = env("SECRET_KEY")

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
    "mdeditor",
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
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_configuration",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
AWS_DEFAULT_ACL="public-read"
AWS_S3_OBJECT_PARAMETERS={"CacheControl": "max-age=86400"}
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

# ---------------------------------------------------------------------------
# Static and media
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = f"{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/"

WHITENOISE_MAX_AGE = 31536000

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Frame settings for mdeditor
X_FRAME_OPTIONS = "SAMEORIGIN"

MDEDITOR_CONFIGS = {
    "default": {
        "width": "100% ",
        "height": 400,
        "toolbar": [
            "undo",
            "redo",
            "|",
            "bold",
            "del",
            "italic",
            "quote",
            "|",
            "h1",
            "h2",
            "h3",
            "h5",
            "h6",
            "|",
            "list-ul",
            "list-ol",
            "hr",
            "|",
            "link",
            "code",
            "preformatted-text",
            "code-block",
            "||",
            "preview",
            "watch",
            "fullscreen",
        ],
        "upload_image_formats": ["jpg", "jpeg", "gif", "png", "bmp", "webp", "svg"],
        "image_folder": "editor",
        "theme": "default",
        "preview_theme": "default",
        "editor_theme": "default",
        "toolbar_autofixed": False,
        "search_replace": True,
        "emoji": True,
        "tex": True,
        "flow_chart": True,
        "sequence": True,
        "watch": True,
        "lineWrapping": True,
        "lineNumbers": True,
        "language": "en",
    }
}
