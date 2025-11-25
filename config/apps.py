from django.contrib.admin.apps import AdminConfig
from django.contrib import admin
from django.contrib.admin import sites

from config.admin import CustomAdminSite


class CustomAdminConfig(AdminConfig):
    default_site = "config.admin.CustomAdminSite"

    def ready(self):
        custom_site = CustomAdminSite()
        admin.site = custom_site
        sites.site = custom_site

        # Register models using our custom site
        super().ready()
