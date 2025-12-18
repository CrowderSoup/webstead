from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin

from solo.admin import SingletonModelAdmin

from files.admin import AttachmentInline
from .models import (
    Page,
    Menu,
    MenuItem,
    SiteConfiguration,
    Redirect,
    HCard,
    HCardEmail,
    HCardUrl,
    HCardPhoto,
    HCardLogo,
    HCardTel,
    HCardCategory,
    HCardImpp,
    HCardKey,
    ThemeInstall,
)
from .themes import discover_themes
from .widgets import CodeMirrorTextarea


class SiteConfigurationAdminForm(forms.ModelForm):
    active_theme = forms.ChoiceField(required=False)

    class Meta:
        model = SiteConfiguration
        fields = "__all__"
        widgets = {
            "robots_txt": CodeMirrorTextarea(mode="text/plain"),
        }

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
                    "site_author",
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
admin.site.register(Redirect)


class PageAdminForm(forms.ModelForm):
    class Meta:
        model = Page
        fields = "__all__"
        widgets = {"content": CodeMirrorTextarea()}


@admin.register(Page)
class PageAdmin(ModelAdmin):
    form = PageAdminForm
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]


class HCardEmailInline(admin.TabularInline):
    model = HCardEmail
    extra = 1


class HCardUrlInline(admin.TabularInline):
    model = HCardUrl
    extra = 1
    fields = ("value", "kind")


class HCardPhotoInline(admin.TabularInline):
    model = HCardPhoto
    extra = 1


class HCardLogoInline(admin.TabularInline):
    model = HCardLogo
    extra = 1


class HCardTelInline(admin.TabularInline):
    model = HCardTel
    extra = 1


class HCardCategoryInline(admin.TabularInline):
    model = HCardCategory
    extra = 1


class HCardImppInline(admin.TabularInline):
    model = HCardImpp
    extra = 1


class HCardKeyInline(admin.TabularInline):
    model = HCardKey
    extra = 1


class HCardAdminForm(forms.ModelForm):
    class Meta:
        model = HCard
        fields = "__all__"
        widgets = {
            "note": CodeMirrorTextarea(),
        }


@admin.register(HCard)
class HCardAdmin(ModelAdmin):
    form = HCardAdminForm
    list_display = ("name", "user", "org_name", "updated_at")
    search_fields = (
        "name",
        "given_name",
        "family_name",
        "nickname",
        "emails__value",
        "urls__value",
    )
    list_filter = ("user", "org_name")
    inlines = [
        HCardEmailInline,
        HCardUrlInline,
        HCardPhotoInline,
        HCardLogoInline,
        HCardTelInline,
        HCardCategoryInline,
        HCardImppInline,
        HCardKeyInline,
    ]


@admin.register(ThemeInstall)
class ThemeInstallAdmin(ModelAdmin):
    list_display = ("slug", "source_type", "version", "last_synced_at", "last_sync_status")
    search_fields = ("slug", "source_url")
    list_filter = ("source_type", "last_sync_status")
