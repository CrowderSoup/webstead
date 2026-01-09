from django.urls import path

from . import views

app_name = "site_admin"

urlpatterns = [
    path("login/", views.SiteAdminLoginView.as_view(), name="login"),
    path("logout/", views.SiteAdminLogoutView.as_view(), name="logout"),
    path("bar/", views.admin_bar, name="admin_bar"),
    path("", views.dashboard, name="dashboard"),
    path("analytics/", views.analytics_dashboard, name="analytics_dashboard"),
    path("settings/", views.site_settings, name="site_settings"),
    path("settings/themes/", views.theme_settings, name="theme_settings"),
    path(
        "settings/themes/<slug:slug>/edit/",
        views.theme_file_edit,
        name="theme_file_edit",
    ),
    path(
        "settings/themes/installs/<slug:slug>/",
        views.theme_install_detail,
        name="theme_install_detail",
    ),
    path("profile/", views.profile_edit, name="profile_edit"),
    path("profile/urls/<int:url_id>/delete/", views.profile_url_delete, name="profile_url_delete"),
    path("profile/emails/<int:email_id>/delete/", views.profile_email_delete, name="profile_email_delete"),
    path("profile/upload-photo/", views.profile_upload_photo, name="profile_upload_photo"),
    path("profile/delete-photo/", views.profile_delete_photo, name="profile_delete_photo"),
    path("settings/menus/", views.menu_list, name="menu_list"),
    path("settings/menus/new/", views.menu_edit, name="menu_create"),
    path("settings/menus/<int:menu_id>/", views.menu_edit, name="menu_edit"),
    path(
        "settings/menus/items/<int:item_id>/delete/",
        views.menu_item_delete,
        name="menu_item_delete",
    ),
    path("settings/redirects/", views.redirect_list, name="redirect_list"),
    path("settings/redirects/new/", views.redirect_edit, name="redirect_create"),
    path("settings/redirects/<int:redirect_id>/", views.redirect_edit, name="redirect_edit"),
    path(
        "settings/redirects/<int:redirect_id>/delete/",
        views.redirect_delete,
        name="redirect_delete",
    ),
    path("pages/", views.page_list, name="page_list"),
    path("pages/new/", views.page_edit, name="page_create"),
    path("pages/<slug:slug>/delete/", views.page_delete, name="page_delete"),
    path("pages/<slug:slug>/", views.page_edit, name="page_edit"),
    path("posts/upload-photo/", views.upload_post_photo, name="post_upload_photo"),
    path("posts/delete-photo/", views.delete_post_photo, name="post_delete_photo"),
    path("posts/", views.post_list, name="post_list"),
    path("posts/new/", views.post_edit, name="post_create"),
    path("posts/<slug:slug>/delete/", views.post_delete, name="post_delete"),
    path(
        "posts/<slug:slug>/delete-permanent/",
        views.post_permanent_delete,
        name="post_permanent_delete",
    ),
    path("posts/<slug:slug>/", views.post_edit, name="post_edit"),
    path("webmentions/", views.webmention_list, name="webmention_list"),
    path("webmentions/new/", views.webmention_create, name="webmention_create"),
    path("webmentions/<int:mention_id>/", views.webmention_detail, name="webmention_detail"),
    path(
        "webmentions/<int:mention_id>/resend/",
        views.webmention_resend,
        name="webmention_resend",
    ),
    path(
        "webmentions/<int:mention_id>/approve/",
        views.webmention_approve,
        name="webmention_approve",
    ),
    path(
        "webmentions/<int:mention_id>/reject/",
        views.webmention_reject,
        name="webmention_reject",
    ),
    path(
        "webmentions/<int:mention_id>/delete/",
        views.webmention_delete,
        name="webmention_delete",
    ),
    path("files/", views.file_list, name="file_list"),
    path("files/new/", views.file_create, name="file_create"),
    path("files/<int:file_id>/delete/", views.file_delete, name="file_delete"),
    path("files/<int:file_id>/", views.file_edit, name="file_edit"),
]
