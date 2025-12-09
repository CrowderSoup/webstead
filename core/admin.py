from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin

from solo.admin import SingletonModelAdmin

from files.admin import AttachmentInline
from .models import Page, Menu, MenuItem, SiteConfiguration, Elsewhere, Redirect
from .themes import discover_themes


class SiteConfigurationAdminForm(forms.ModelForm):
    active_theme = forms.ChoiceField(required=False)

    class Meta:
        model = SiteConfiguration
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        available_themes = discover_themes()
        choices = [("", "Default theme (built-in templates)")]
        for theme in available_themes:
            label = theme.label
            if theme.version:
                label = f"{label} ({theme.version})"
            choices.append((theme.slug, label))
        self.fields["active_theme"].choices = choices


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(SingletonModelAdmin):
    form = SiteConfigurationAdminForm
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "title",
                    "tagline",
                    "intro",
                    "bio",
                    "active_theme",
                )
            },
        ),
        (
            "Navigation",
            {
                "fields": (
                    "main_menu",
                    "footer_menu",
                )
            },
        ),
        (
            "Robots.txt",
            {
                "fields": ("robots_txt",),
            },
        ),
    )

admin.site.register(Menu)
admin.site.register(MenuItem)
admin.site.register(Elsewhere)
admin.site.register(Redirect)

@admin.register(Page)
class PageAdmin(ModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]
