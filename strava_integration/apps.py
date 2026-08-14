from django.apps import AppConfig


class StravaIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "strava_integration"
    label = "strava_integration"
    verbose_name = "Strava"
