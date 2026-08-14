from django.urls import path

from . import views

urlpatterns = [
    path("strava/auth/start/", views.oauth_start, name="strava_oauth_start"),
    path("strava/auth/callback/", views.oauth_callback, name="strava_oauth_callback"),
    path("strava/webhook/", views.webhook_receive, name="strava_webhook"),
]
