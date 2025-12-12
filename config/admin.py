from functools import partial

from django.urls import path, reverse

from unfold.sites import UnfoldAdminSite

from core import admin_views


class CustomAdminSite(UnfoldAdminSite):
    """
    Hide the auth app from the admin dashboard and navigation sidebar.
    """

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "themes/",
                self.admin_view(partial(admin_views.theme_manager, admin_site=self)),
                name="core_theme_list",
            ),
            path(
                "themes/<slug:slug>/edit/",
                self.admin_view(partial(admin_views.theme_file_edit, admin_site=self)),
                name="core_theme_edit",
            ),
        ]
        return custom_urls + urls

    def get_app_list(self, request, app_label=None):
        app_list = super().get_app_list(request, app_label)
        app_list = [app for app in app_list if app["app_label"] != "auth"]

        try:
            theme_url = reverse("admin:core_theme_list")
            app_list.append(
                {
                    "app_label": "themes",
                    "name": "Themes",
                    "app_url": theme_url,
                    "has_module_perms": True,
                    "models": [
                        {
                            "name": "Theme manager",
                            "object_name": "Theme",
                            "perms": {"view": True},
                            "admin_url": theme_url,
                            "view_only": True,
                        }
                    ],
                }
            )
        except Exception:
            pass

        return app_list
