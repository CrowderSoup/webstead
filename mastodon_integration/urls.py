from django.urls import path

from . import views

urlpatterns = [
    path("mastodon/auth/start/", views.oauth_start, name="mastodon_oauth_start"),
    path("mastodon/auth/callback/", views.oauth_callback, name="mastodon_oauth_callback"),
]
