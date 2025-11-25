from unfold.sites import UnfoldAdminSite


class CustomAdminSite(UnfoldAdminSite):
    """
    Hide the auth app from the admin dashboard and navigation sidebar.
    """

    def get_app_list(self, request, app_label=None):
        app_list = super().get_app_list(request, app_label)
        return [app for app in app_list if app["app_label"] != "auth"]
