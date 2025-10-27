from django.contrib import admin

from files.admin import AttachmentInline
from .models import Post, Tag

@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]

admin.site.register(Tag)
