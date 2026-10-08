from django.urls import path

from . import views

urlpatterns = [
    path("mcp", views.mcp_endpoint, name="mcp"),
    path("mcp/", views.mcp_endpoint),
]
