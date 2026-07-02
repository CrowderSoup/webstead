from django import template

register = template.Library()

_NAV_SECTIONS = {
    "dashboard": {
        "dashboard",
    },
    "posts": {
        "post_list", "post_create", "post_edit", "post_delete",
        "post_permanent_delete", "post_upload_photo", "post_delete_photo",
        "nearby_checkin_places",
    },
    "pages": {
        "page_list", "page_create", "page_edit", "page_delete",
    },
    "files": {
        "file_list", "file_create", "file_edit", "file_delete",
    },
    "interactions": {
        "interactions",
        "comment_list", "comment_detail", "comment_approve",
        "comment_mark_spam", "comment_delete",
        "webmention_list", "webmention_create", "webmention_detail",
        "webmention_resend", "webmention_approve", "webmention_reject",
        "webmention_delete",
    },
    "analytics": {
        "analytics_dashboard", "analytics_user_agents", "analytics_bot_detection",
        "analytics_ignored_user_agents", "analytics_mark_false_positive_user_agent",
        "analytics_unmark_false_positive_user_agent", "analytics_ignored_user_agents_export",
        "analytics_errors_by_user_agent", "analytics_ignore_user_agent",
        "analytics_ignore_user_agents_bulk", "analytics_unignore_user_agent",
        "analytics_delete_error",
        "redirect_list", "redirect_create", "redirect_edit", "redirect_delete",
    },
    "settings": {
        "site_settings",
        "theme_settings", "theme_git_refs", "theme_file_edit", "theme_install_detail",
        "menu_list", "menu_create", "menu_edit", "menu_item_delete",
        "plugin_list", "plugin_install", "plugin_restart_status", "plugin_update", "plugin_remove",
        "widget_list", "widget_add", "widget_reorder", "widget_edit", "widget_delete",
        "indieauth_settings", "indieauth_client_create", "indieauth_client_detail", "indieauth_client_edit",
        "microsub_channel_list", "microsub_channel_create", "microsub_channel_reorder",
        "microsub_channel_detail", "microsub_channel_edit", "microsub_channel_delete",
        "microsub_channel_mark_read", "microsub_feed_add", "microsub_feed_remove", "microsub_import_opml",
        "mastodon_settings", "mastodon_disconnect", "mastodon_manual_sync",
        "error_log_list", "error_log_detail",
        "task_log_list", "task_log_detail",
        "micropub_error_list", "micropub_error_detail",
        "indieauth_error_list", "indieauth_error_detail",
    },
    "profile": {
        "profile_edit", "profile_url_delete", "profile_email_delete",
        "profile_upload_photo", "profile_delete_photo",
    },
}


@register.simple_tag(takes_context=True)
def nav_active(context, section):
    """
    Output 'site-admin-side-link--active' if the current URL name belongs
    to the given navigation section, otherwise output an empty string.

    Usage: class="site-admin-side-link {% nav_active 'settings' %}"
    """
    current = context.get("current", "")
    if current and current in _NAV_SECTIONS.get(section, set()):
        return "site-admin-side-link--active"
    return ""
