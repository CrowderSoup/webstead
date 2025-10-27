from django.contrib import admin

from solo.admin import SingletonModelAdmin

from files.admin import AttachmentInline
from .models import Page, Menu, MenuItem, SiteConfiguration

admin.site.register(SiteConfiguration, SingletonModelAdmin)
admin.site.register(Menu)
admin.site.register(MenuItem)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]
