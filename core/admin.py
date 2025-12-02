from django.contrib import admin
from unfold.admin import ModelAdmin

from solo.admin import SingletonModelAdmin

from files.admin import AttachmentInline
from .models import Page, Menu, MenuItem, SiteConfiguration, Elsewhere, Redirect

admin.site.register(SiteConfiguration, SingletonModelAdmin)
admin.site.register(Menu)
admin.site.register(MenuItem)
admin.site.register(Elsewhere)
admin.site.register(Redirect)

@admin.register(Page)
class PageAdmin(ModelAdmin):
    prepopulated_fields = {"slug": ("title",)}
    inlines = [AttachmentInline]
