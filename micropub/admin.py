from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Webmention


@admin.register(Webmention)
class WebmentionAdmin(ModelAdmin):
    list_display = [
        "source",
        "target",
        "mention_type",
        "status",
        "target_post",
        "created_at",
    ]
    list_filter = ["status", "mention_type"]
    search_fields = ["source", "target"]
