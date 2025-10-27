from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from .models import File, Attachment

class AttachmentInline(GenericTabularInline):
    model = Attachment
    extra = 1
    fields = ("asset", "role", "sort_order")
    autocomplete_fields = ("asset",)
    show_change_link = True

@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "created_at")
    search_fields = ("alt_text", "caption", "file")
