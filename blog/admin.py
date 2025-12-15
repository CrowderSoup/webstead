from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin

from files.admin import AttachmentInline
from core.widgets import CodeMirrorTextarea
from .models import Post, Tag


class PostAdminForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = "__all__"
        widgets = {"content": CodeMirrorTextarea()}


@admin.register(Post)
class PostAdmin(ModelAdmin):
    form = PostAdminForm
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]

admin.site.register(Tag)
