from django.contrib import admin

from .models import Visit


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = (
        "path",
        "user",
        "session_key",
        "ip_address",
        "started_at",
        "duration_seconds",
    )
    list_filter = (
        ("user", admin.EmptyFieldListFilter),
        "started_at",
    )
    search_fields = ("path", "ip_address", "session_key", "user__username")
