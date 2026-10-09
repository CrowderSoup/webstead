from django.urls import path

from . import views

urlpatterns = [
    path(".well-known/oauth-authorization-server", views.metadata, name="indieauth-oauth-authorization-server"),
    path("indieauth/metadata", views.metadata, name="indieauth-metadata"),
    path("indieauth/authorize", views.authorize, name="indieauth-authorize"),
    path("indieauth/token", views.token, name="indieauth-token"),
    path("indieauth/introspect", views.introspect, name="indieauth-introspect"),
    path("indieauth/revoke", views.revoke, name="indieauth-revoke"),
    path("indieauth/userinfo", views.userinfo, name="indieauth-userinfo"),
]
