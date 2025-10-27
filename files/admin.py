from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from .models import File, Attachment

class AttachmentInline(GenericTabularInline):
    model = Attachment
    extra = 0

@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "created_at")
