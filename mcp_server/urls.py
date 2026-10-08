from django.urls import path

from . import views

urlpatterns = [
    path("mcp", views.mcp_endpoint, name="mcp"),
    path("mcp/", views.mcp_endpoint),
    path(
        ".well-known/oauth-protected-resource/mcp",
        views.protected_resource_metadata,
        name="mcp-protected-resource",
    ),
    path(
        ".well-known/oauth-protected-resource",
        views.protected_resource_metadata,
        {"at_root": True},
        name="mcp-protected-resource-root",
    ),
]
