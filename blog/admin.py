from django.contrib import admin
from unfold.admin import ModelAdmin

from files.admin import AttachmentInline
from .models import Post, Tag

@admin.register(Post)
class PostAdmin(ModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]

admin.site.register(Tag)
