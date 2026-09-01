from django import forms
import re
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.utils import timezone
from django.utils.text import slugify

from analytics.bot_detection import validate_bot_pattern
from analytics.models import UserAgentBotRule
from blog.models import Comment, Post, Tag
from indieauth.models import IndieAuthClient
from core.models import (
    HCard,
    HCardEmail,
    HCardUrl,
    Menu,
    MenuItem,
    Page,
    Redirect,
    SiteConfiguration,
)
from files.models import File
from micropub.models import Webmention
from core.themes import discover_themes
from core.widgets import EasyMDETextarea


class PostFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    kind = forms.ChoiceField(
        required=False,
        choices=[("", "All kinds"), *Post.KIND_CHOICES],
        label="Kind",
    )
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any status"),
            ("draft", "Draft"),
            ("scheduled", "Scheduled"),
            ("published", "Published"),
            ("deleted", "Deleted"),
        ],
        label="Status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs.setdefault(
            "placeholder", "Search titles, content, or slugs"
        )
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class PostForm(forms.ModelForm):
    PRIMARY_KIND_CHOICES = [
        (Post.NOTE, "Note"),
        (Post.ARTICLE, "Article"),
        (Post.PHOTO, "Photo"),
    ]
    MORE_KIND_GROUPS = [
        (
            "Respond",
            [
                (Post.REPLY, "Reply"),
                (Post.LIKE, "Like"),
                (Post.REPOST, "Repost"),
                (Post.RSVP, "RSVP"),
            ],
        ),
        (
            "Places & events",
            [
                (Post.CHECKIN, "Check-in"),
                (Post.ACTIVITY, "Activity"),
                (Post.EVENT, "Event"),
            ],
        ),
        ("Save", [(Post.BOOKMARK, "Bookmark")]),
    ]
    publishing_action = forms.ChoiceField(
        required=False,
        choices=[
            ("save", "Save changes"),
            ("draft", "Save as draft"),
            ("publish", "Publish now"),
            ("schedule", "Schedule"),
        ],
        widget=forms.HiddenInput(),
    )
    tags_text = forms.CharField(
        required=False,
        label="Tags",
        help_text="Press Enter or comma to add tags.",
    )
    activity_type = forms.CharField(
        required=False,
        label="Activity type",
        help_text="Describe the activity (hike, bike ride, run, etc.).",
    )
    save_as_draft = forms.BooleanField(
        required=False,
        label="Save as draft",
        help_text="Leaves publish time empty.",
    )
    # Event fields
    event_start = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Start",
    )
    event_end = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="End",
    )
    event_location = forms.CharField(required=False, label="Location")
    event_url = forms.URLField(required=False, label="Event URL")
    # RSVP fields
    rsvp_value = forms.ChoiceField(
        required=False,
        choices=[("", "---"), ("yes", "Yes"), ("no", "No"), ("maybe", "Maybe"), ("interested", "Interested")],
        label="RSVP",
    )
    # Check-in fields
    checkin_name = forms.CharField(required=False, label="Place name")
    checkin_latitude = forms.FloatField(required=False, label="Latitude")
    checkin_longitude = forms.FloatField(required=False, label="Longitude")
    field_order = [
        "title",
        "slug",
        "kind",
        "content",
        "activity_type",
        "event_start",
        "event_end",
        "event_location",
        "event_url",
        "rsvp_value",
        "checkin_name",
        "checkin_latitude",
        "checkin_longitude",
        "tags_text",
        "publishing_action",
        "save_as_draft",
        "published_on",
        "deleted",
        "like_of",
        "repost_of",
        "in_reply_to",
        "bookmark_of",
    ]
    published_on = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Publish time",
    )

    class Meta:
        model = Post
        fields = [
            "title",
            "slug",
            "kind",
            "content",
            "published_on",
            "deleted",
            "like_of",
            "repost_of",
            "in_reply_to",
            "bookmark_of",
            "mastodon_syndicate",
        ]
        widgets = {
            "content": EasyMDETextarea(),
            "like_of": forms.URLInput(attrs={"placeholder": "https://"}),
            "repost_of": forms.URLInput(attrs={"placeholder": "https://"}),
            "in_reply_to": forms.URLInput(attrs={"placeholder": "https://"}),
            "bookmark_of": forms.URLInput(attrs={"placeholder": "https://"}),
            "mastodon_syndicate": forms.Select(
                choices=[
                    ("", "Default (use per-kind setting)"),
                    ("true", "Publish to Mastodon"),
                    ("false", "Don't publish to Mastodon"),
                ]
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.primary_kind_choices = self.PRIMARY_KIND_CHOICES
        self.more_kind_groups = self.MORE_KIND_GROUPS
        if not self.instance.pk and not self.is_bound:
            self.initial["kind"] = Post.NOTE
        self.fields["title"].required = False
        self.fields["slug"].required = False
        self.fields["content"].required = False
        self.fields["activity_type"].required = False
        self.fields["save_as_draft"].initial = not bool(self.instance.published_on)
        self.fields["publishing_action"].initial = (
            "save" if self.instance.published_on else "draft"
        )
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
                )
            if name == "activity_type":
                field.widget.attrs.setdefault("list", "activity-type-list")
                field.widget.attrs.setdefault("placeholder", "Hike")
            if name == "tags_text":
                field.widget.attrs.setdefault("class", "tag-input__field")
                field.widget.attrs.setdefault("placeholder", "Add tags")
                field.widget.attrs.setdefault("autocomplete", "off")
                field.widget.attrs.setdefault("spellcheck", "false")
        self.fields["slug"].widget.attrs.setdefault(
            "data-slug-source", "input[name='title']"
        )
        if self.instance.pk:
            self.fields["tags_text"].initial = ", ".join(
                self.instance.tags.values_list("tag", flat=True)
            )
            activity_type = _activity_type_from_mf2(self.instance.mf2)
            if activity_type:
                self.fields["activity_type"].initial = activity_type
            mf2 = self.instance.mf2 if isinstance(self.instance.mf2, dict) else {}
            # Event
            event_data = mf2.get("event") or {}
            if isinstance(event_data, dict):
                self.fields["event_start"].initial = event_data.get("start", "")
                self.fields["event_end"].initial = event_data.get("end", "")
                self.fields["event_location"].initial = event_data.get("location", "")
                self.fields["event_url"].initial = event_data.get("url", "")
            # RSVP
            self.fields["rsvp_value"].initial = mf2.get("rsvp", "")
            # Check-in
            checkin_data = mf2.get("checkin") or {}
            if isinstance(checkin_data, dict):
                self.fields["checkin_name"].initial = checkin_data.get("name", "")
                self.fields["checkin_latitude"].initial = checkin_data.get("latitude")
                self.fields["checkin_longitude"].initial = checkin_data.get("longitude")
        # Mastodon syndicate select: map bool/None model value to string choice
        if self.instance.pk:
            ms = self.instance.mastodon_syndicate
            if ms is True:
                self.fields["mastodon_syndicate"].initial = "true"
            elif ms is False:
                self.fields["mastodon_syndicate"].initial = "false"
            else:
                self.fields["mastodon_syndicate"].initial = ""
        if self.instance.pk and self.instance.published_on:
            local_time = timezone.localtime(self.instance.published_on)
            self.fields["published_on"].initial = local_time.strftime("%Y-%m-%dT%H:%M")

    def clean_mastodon_syndicate(self):
        """Convert the Select string value back to True / False / None."""
        value = self.cleaned_data.get("mastodon_syndicate")
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    def clean_published_on(self):
        value = self.cleaned_data.get("published_on")
        if self.cleaned_data.get("save_as_draft"):
            return None
        if value and timezone.is_naive(value):
            return timezone.make_aware(value)
        return value

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("publishing_action") == "schedule"
            and not cleaned_data.get("published_on")
        ):
            self.add_error(
                "published_on", "Choose a date and time to schedule this post."
            )
        elif (
            cleaned_data.get("publishing_action") == "schedule"
            and cleaned_data.get("published_on") <= timezone.now()
        ):
            self.add_error("published_on", "Choose a future date and time.")
        return cleaned_data

    def clean_tags_text(self):
        tags_text = self.cleaned_data.get("tags_text", "")
        tags = [slugify(tag.strip()) for tag in tags_text.split(",") if tag.strip()]
        return ",".join(dict.fromkeys(tags))

    def save_tags(self, post):
        tags_text = self.cleaned_data.get("tags_text", "")
        tag_slugs = [tag for tag in tags_text.split(",") if tag]
        tags = []
        for tag_slug in tag_slugs:
            tag, _ = Tag.objects.get_or_create(tag=tag_slug)
            tags.append(tag)
        post.tags.set(tags)


def _activity_type_from_mf2(mf2_data):
    if not isinstance(mf2_data, dict):
        return ""
    activity_items = mf2_data.get("activity") or []
    if not activity_items:
        return ""
    activity_item = activity_items[0] if isinstance(activity_items, list) else activity_items
    if not isinstance(activity_item, dict):
        return ""
    properties = activity_item.get("properties")
    if not isinstance(properties, dict):
        return ""
    for key in ("activity-type", "name", "category"):
        values = properties.get(key) or []
        if values:
            return str(values[0])
    return ""


class PageFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.SearchInput(
            attrs={"placeholder": "Search titles, content, or slugs"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class WebmentionFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status"), *Webmention.STATUS_CHOICES],
        label="Status",
    )
    direction = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any direction"),
            ("incoming", "Incoming"),
            ("outgoing", "Outgoing"),
        ],
        label="Direction",
    )
    mention_type = forms.ChoiceField(
        required=False,
        choices=[("", "Any type"), *Webmention.MENTION_CHOICES],
        label="Type",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class ErrorLogFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    source = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any type"),
            ("micropub", "Micropub"),
            ("indieauth", "IndieAuth"),
        ],
        label="Type",
    )
    status_code = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any status"),
            ("400", "400"),
            ("401", "401"),
            ("403", "403"),
            ("404", "404"),
            ("405", "405"),
            ("415", "415"),
        ],
        label="Status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class TaskLogFilterForm(forms.Form):
    task = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All tasks"),
            ("analytics.tasks.lookup_user_agent", "analytics.tasks.lookup_user_agent"),
            ("core.tasks.reconcile_themes", "core.tasks.reconcile_themes"),
            ("core.tasks.sync_themes", "core.tasks.sync_themes"),
            ("micropub.tasks.dispatch_webmentions", "micropub.tasks.dispatch_webmentions"),
            ("microsub.tasks.poll_microsub_feeds", "microsub.tasks.poll_microsub_feeds"),
        ],
        label="Task",
    )
    worker = forms.CharField(required=False, label="Worker")
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All statuses"),
            ("SUCCESS", "Success"),
            ("FAILURE", "Failure"),
            ("STARTED", "Started"),
            ("RETRY", "Retry"),
            ("REVOKED", "Revoked"),
        ],
        label="Status",
    )
    date_from = forms.DateField(required=False, label="From", widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, label="To", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class IndieAuthFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    client_id = forms.CharField(required=False, label="Client")
    me = forms.CharField(required=False, label="Me")
    user = forms.CharField(required=False, label="User")
    scope = forms.CharField(required=False, label="Scope")
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Any status"),
            ("active", "Active"),
            ("revoked", "Revoked"),
        ],
        label="Status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class CommentFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status"), *Comment.STATUS_CHOICES],
        label="Status",
    )
    post = forms.ModelChoiceField(
        required=False,
        queryset=Post.objects.none(),
        label="Post",
    )
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="From",
    )
    end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="To",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["post"].queryset = Post.objects.order_by("-published_on", "-id")
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class WebmentionCreateForm(forms.Form):
    source = forms.URLField(label="Source URL")
    target = forms.URLField(label="Target URL")
    mention_type = forms.ChoiceField(
        choices=Webmention.MENTION_CHOICES,
        label="Type",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )
        self.fields["source"].widget.attrs.setdefault("placeholder", "https://")
        self.fields["target"].widget.attrs.setdefault("placeholder", "https://")


class PageForm(forms.ModelForm):
    field_order = ["title", "slug", "content", "published_on", "is_gallery"]
    published_on = forms.DateTimeField(
        required=True,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Publish time",
    )

    class Meta:
        model = Page
        fields = ["title", "slug", "content", "published_on", "is_gallery"]
        widgets = {
            "content": EasyMDETextarea(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
                )
        self.fields["slug"].widget.attrs.setdefault(
            "data-slug-source", "input[name='title']"
        )
        if self.instance.pk and self.instance.published_on:
            local_time = timezone.localtime(self.instance.published_on)
            self.fields["published_on"].initial = local_time.strftime(
                "%Y-%m-%dT%H:%M"
            )
        elif not self.instance.pk and not self.initial.get("published_on"):
            self.fields["published_on"].widget.attrs["data-default-now"] = "true"

    def clean_published_on(self):
        value = self.cleaned_data.get("published_on")
        if value and timezone.is_naive(value):
            return timezone.make_aware(value)
        return value


class FileForm(forms.ModelForm):
    class Meta:
        model = File
        fields = ["kind", "file", "alt_text", "caption", "owner"]
        widgets = {
            "caption": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
                )


class SiteConfigurationForm(forms.ModelForm):
    active_theme = forms.ChoiceField(required=False, label="Active theme")
    favicon = forms.ModelChoiceField(
        queryset=File.objects.none(),
        required=False,
        label="Favicon",
        help_text="Pick an uploaded image to use as the site favicon.",
    )

    class Meta:
        model = SiteConfiguration
        fields = [
            "title",
            "tagline",
            "site_url",
            "home_page",
            "favicon",
            "site_author",
            "active_theme",
            "main_menu",
            "footer_menu",
            "default_feed_kinds",
            "microsub_unfollow_removes_entries",
            "activity_units",
            "comments_enabled",
            "developer_tools_enabled",
            "bridgy_publish_bluesky",
            "bridgy_publish_flickr",
            "bridgy_publish_github",
            "robots_txt",
        ]
        widgets = {
            "robots_txt": EasyMDETextarea(),
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
        self.fields["favicon"].queryset = File.objects.filter(kind=File.IMAGE).order_by(
            "-created_at"
        )
        self.fields["home_page"].queryset = Page.objects.order_by("title")
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
                )


class ThemeUploadForm(forms.Form):
    archive = forms.FileField(
        help_text="Upload a .zip containing theme.json plus templates/ and static/ folders."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archive"].widget.attrs.setdefault(
            "class",
            "block w-full text-sm text-(--admin-ink) file:mr-4 file:rounded-full file:border-0 file:bg-(--admin-bg) file:px-4 file:py-2 file:text-sm file:font-semibold file:text-(--admin-ink) hover:file:bg-(--admin-border)",
        )

    def clean_archive(self):
        file = self.cleaned_data["archive"]
        if not file.name.lower().endswith(".zip"):
            raise ValidationError("Only .zip theme archives are supported.")
        return file


class ThemeGitInstallForm(forms.Form):
    git_url = forms.URLField(
        label="Git URL",
        help_text="Provide a public git URL (https) for the theme repository.",
    )
    slug = forms.SlugField(label="Theme slug")
    ref = forms.CharField(
        label="Git ref",
        required=False,
        help_text="Optional branch, tag, or commit to checkout.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("git_url", "slug", "ref"):
            self.fields[name].widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )
        self.fields["slug"].widget.attrs.setdefault(
            "data-slug-source", "input[name='git_url']"
        )
        self.fields["slug"].widget.attrs.setdefault("data-slug-source-kind", "url")
        self.fields["git_url"].widget.attrs.setdefault("data-git-url-input", "true")
        self.fields["ref"].widget.attrs.setdefault("data-git-ref-input", "true")
        self.fields["ref"].widget.attrs.setdefault("list", "theme-git-ref-options")


class ThemeFileForm(forms.Form):
    theme = forms.ChoiceField(label="Theme")
    path = forms.ChoiceField(label="File")
    content = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 25,
                "class": "w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 font-mono text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            }
        )
    )

    def __init__(self, theme_choices, path_choices, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["theme"].choices = theme_choices
        self.fields["path"].choices = [(path, path) for path in path_choices]
        if not path_choices:
            self.fields["path"].required = False
        self.fields["theme"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["path"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )


class ThemeSettingsForm(forms.Form):
    def __init__(self, schema: dict, *args, **kwargs):
        self.schema = schema or {}
        initial = kwargs.get("initial")
        if isinstance(initial, dict):
            initial = dict(initial)
            fields = self.schema.get("fields")
            if isinstance(fields, dict):
                for name, definition in fields.items():
                    if not isinstance(definition, dict):
                        continue
                    if definition.get("type") != "color_alpha":
                        continue
                    value = initial.get(name)
                    if isinstance(value, str):
                        hex_value, alpha = _rgba_to_hex_alpha(value)
                        initial[name] = [hex_value, alpha]
                    elif value is None:
                        initial[name] = ["#000000", 1.0]
            kwargs["initial"] = initial
        super().__init__(*args, **kwargs)
        fields = self.schema.get("fields")
        if not isinstance(fields, dict):
            return
        for name, definition in fields.items():
            if not isinstance(definition, dict):
                continue
            field_type = definition.get("type", "string")
            label = definition.get("label") or name.replace("_", " ").title()
            required = bool(definition.get("required"))
            help_text = definition.get("help") or ""
            form_field = _theme_settings_field(
                field_type,
                label=label,
                required=required,
                help_text=help_text,
                definition=definition,
            )
            if form_field is not None:
                self.fields[name] = form_field


def _theme_settings_field(field_type, *, label, required, help_text, definition):
    if field_type == "text":
        field = forms.CharField(
            required=required,
            label=label,
            help_text=help_text,
            widget=forms.Textarea(attrs={"rows": 4}),
        )
    elif field_type == "boolean":
        field = forms.BooleanField(
            required=False,
            label=label,
            help_text=help_text,
        )
    elif field_type == "number":
        field = forms.FloatField(
            required=required,
            label=label,
            help_text=help_text,
        )
        _apply_theme_settings_number_attrs(field, definition)
    elif field_type == "color":
        field = forms.CharField(
            required=required,
            label=label,
            help_text=help_text,
            widget=forms.TextInput(attrs={"type": "color"}),
        )
    elif field_type == "color_alpha":
        field = ColorAlphaField(
            required=required,
            label=label,
            help_text=help_text,
        )
    elif field_type == "select":
        choices = _theme_settings_choices(definition.get("choices"))
        if not choices:
            return None
        field = forms.ChoiceField(
            required=required,
            label=label,
            help_text=help_text,
            choices=choices,
        )
    else:
        field = forms.CharField(
            required=required,
            label=label,
            help_text=help_text,
        )

    if isinstance(field.widget, forms.CheckboxInput):
        field.widget.attrs.setdefault(
            "class",
            "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
        )
    elif isinstance(field.widget, forms.MultiWidget):
        for subwidget in field.widget.widgets:
            subwidget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )
    else:
        field.widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
    return field


_RGBA_RE = re.compile(
    r"rgba?\(\s*(?P<r>\d{1,3})\s*,\s*(?P<g>\d{1,3})\s*,\s*(?P<b>\d{1,3})(?:\s*,\s*(?P<a>[\d.]+))?\s*\)"
)


def _clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def _rgba_to_hex_alpha(value):
    if not isinstance(value, str):
        return "#000000", 1.0
    match = _RGBA_RE.fullmatch(value.strip())
    if not match:
        return value.strip(), 1.0
    r = _clamp(int(match.group("r")), 0, 255)
    g = _clamp(int(match.group("g")), 0, 255)
    b = _clamp(int(match.group("b")), 0, 255)
    alpha = match.group("a")
    a = float(alpha) if alpha is not None else 1.0
    a = _clamp(a, 0.0, 1.0)
    return f"#{r:02x}{g:02x}{b:02x}", a


def _hex_to_rgb(value):
    if not isinstance(value, str):
        return 0, 0, 0
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return 0, 0, 0
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return 0, 0, 0


class ColorHexInput(forms.TextInput):
    input_type = "color"

    def format_value(self, value):
        if value is None:
            return None
        hex_value, _alpha = _rgba_to_hex_alpha(value)
        return hex_value


class ColorAlphaWidget(forms.MultiWidget):
    def __init__(self, attrs=None):
        widgets = [
            ColorHexInput(),
            forms.NumberInput(attrs={"min": "0", "max": "1", "step": "0.01"}),
        ]
        super().__init__(widgets, attrs)

    def decompress(self, value):
        if value is None:
            return ["#000000", 1.0]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            hex_value, alpha = _rgba_to_hex_alpha(value[0])
            try:
                alpha = float(value[1])
            except (TypeError, ValueError):
                alpha = alpha
            return [hex_value, _clamp(alpha, 0.0, 1.0)]
        hex_value, alpha = _rgba_to_hex_alpha(value)
        return [hex_value, alpha]

    def format_value(self, value):
        if value is None:
            return [None, None]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return list(value)
        return self.decompress(value)

    def get_context(self, name, value, attrs):
        decompressed = self.decompress(value)
        context = super().get_context(name, decompressed, attrs)
        subwidgets = context.get("widget", {}).get("subwidgets", [])
        if len(subwidgets) >= 2:
            subwidgets[0]["value"] = decompressed[0]
            subwidgets[1]["value"] = f"{float(decompressed[1]):.2f}" if decompressed[1] is not None else "1.00"
        return context


class ColorAlphaField(forms.Field):
    widget = ColorAlphaWidget

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widget = ColorAlphaWidget()

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            hex_value, alpha = _rgba_to_hex_alpha(value[0])
            try:
                alpha = float(value[1])
            except (TypeError, ValueError):
                pass
            return [hex_value, f"{_clamp(alpha, 0.0, 1.0):.2f}"]
        hex_value, alpha = _rgba_to_hex_alpha(value)
        return [hex_value, f"{_clamp(alpha, 0.0, 1.0):.2f}"]

    def to_python(self, value):
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            hex_value = value[0] or "#000000"
            alpha = value[1]
        else:
            hex_value, alpha = _rgba_to_hex_alpha(value)
        try:
            alpha = float(alpha)
        except (TypeError, ValueError):
            alpha = 1.0
        alpha = _clamp(alpha, 0.0, 1.0)
        r, g, b = _hex_to_rgb(hex_value)
        return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def _theme_settings_choices(raw_choices):
    if not isinstance(raw_choices, list):
        return []
    choices = []
    for item in raw_choices:
        if isinstance(item, dict):
            value = item.get("value")
            label = item.get("label", value)
        else:
            value = item
            label = item
        if value is None:
            continue
        choices.append((value, label))
    return choices


def _apply_theme_settings_number_attrs(field, definition):
    attrs = field.widget.attrs
    attrs.setdefault("type", "number")
    if isinstance(definition.get("min"), (int, float)):
        attrs.setdefault("min", str(definition["min"]))
    if isinstance(definition.get("max"), (int, float)):
        attrs.setdefault("max", str(definition["max"]))
    if isinstance(definition.get("step"), (int, float)):
        attrs.setdefault("step", str(definition["step"]))


class MenuForm(forms.ModelForm):
    class Meta:
        model = Menu
        fields = ["title"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )


class MenuItemForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = ["text", "url", "weight"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["text"].widget.attrs.setdefault("placeholder", "Label")
        self.fields["url"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["url"].widget.attrs.setdefault("placeholder", "/about or https://...")
        self.fields["weight"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["weight"].widget.attrs.setdefault("min", "0")
        self.fields["weight"].widget.attrs.setdefault("inputmode", "numeric")


class RedirectForm(forms.ModelForm):
    class Meta:
        model = Redirect
        fields = ["from_path", "to_path", "redirect_type"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_path"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["from_path"].widget.attrs.setdefault("placeholder", "/old/")
        self.fields["to_path"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["to_path"].widget.attrs.setdefault("placeholder", "/new/ or https://...")
        self.fields["redirect_type"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )


class UserAgentBotRuleForm(forms.ModelForm):
    class Meta:
        model = UserAgentBotRule
        fields = ["enabled", "pattern"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["enabled"].widget.attrs.setdefault(
            "class",
            "h-4 w-4 rounded border-(--admin-border) text-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["pattern"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs font-mono focus:border-(--admin-accent) focus:ring-(--admin-accent)",
        )
        self.fields["pattern"].widget.attrs.setdefault("rows", 4)
        self.fields["pattern"].widget.attrs.setdefault(
            "placeholder",
            r"(?i)(bot|crawler|spider|headless)",
        )

    def clean(self):
        cleaned_data = super().clean()
        enabled = cleaned_data.get("enabled")
        pattern = cleaned_data.get("pattern", "")
        if enabled and not pattern.strip():
            self.add_error("pattern", "Regex pattern is required when bot detection is enabled.")
            return cleaned_data
        if pattern:
            try:
                validate_bot_pattern(pattern)
            except ValueError as exc:
                self.add_error("pattern", f"Invalid regex: {exc}")
        return cleaned_data


class HCardForm(forms.ModelForm):
    class Meta:
        model = HCard
        fields = [
            "name",
            "nickname",
            "note",
            "uid",
            "org_name",
            "job_title",
            "role",
            "locality",
            "region",
            "country_name",
            "bday",
            "anniversary",
        ]
        widgets = {
            "note": EasyMDETextarea(),
            "bday": forms.DateInput(attrs={"type": "date"}),
            "anniversary": forms.DateInput(attrs={"type": "date"}),
            "uid": forms.URLInput(attrs={"placeholder": "https://"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class HCardUrlForm(forms.ModelForm):
    value = forms.CharField(required=True)

    class Meta:
        model = HCardUrl
        fields = ["value", "kind"]
        widgets = {
            "value": forms.TextInput(attrs={"placeholder": "https:// or name@example.com"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )

    def clean(self):
        cleaned_data = super().clean()
        value = (cleaned_data.get("value") or "").strip()
        kind = cleaned_data.get("kind") or HCardUrl.OTHER
        if not value:
            return cleaned_data
        if kind == HCardUrl.EMAIL:
            validator = EmailValidator()
        else:
            validator = URLValidator()
        try:
            validator(value)
        except ValidationError as exc:
            self.add_error("value", exc.messages)
        else:
            cleaned_data["value"] = value
        return cleaned_data


class HCardEmailForm(forms.ModelForm):
    class Meta:
        model = HCardEmail
        fields = ["value"]
        widgets = {
            "value": forms.TextInput(attrs={"placeholder": "name@example.com"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )


class IndieAuthClientForm(forms.ModelForm):
    redirect_uris_text = forms.CharField(
        required=False,
        label="Redirect URIs",
        help_text="One URI per line. Each must be http or https with no fragment.",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "https://example.com/callback"}),
    )

    class Meta:
        model = IndieAuthClient
        fields = ["client_id", "name", "logo_url"]
        widgets = {
            "client_id": forms.URLInput(attrs={"placeholder": "https://"}),
            "logo_url": forms.URLInput(attrs={"placeholder": "https://"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.redirect_uris:
            self.fields["redirect_uris_text"].initial = "\n".join(
                self.instance.redirect_uris
            )
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)",
            )

    def clean_redirect_uris_text(self):
        raw = self.cleaned_data.get("redirect_uris_text", "")
        uris = [line.strip() for line in raw.splitlines() if line.strip()]
        url_validator = URLValidator(schemes=["http", "https"])
        for uri in uris:
            if "#" in uri:
                raise ValidationError(f"Redirect URI must not contain a fragment: {uri}")
            url_validator(uri)
        return uris

    def save(self, commit=True):
        self.instance.redirect_uris = self.cleaned_data.get("redirect_uris_text", [])
        return super().save(commit=commit)


class PluginGitInstallForm(forms.Form):
    FIELD_CLASS = "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)"

    git_url = forms.URLField(
        label="Git URL",
        help_text="Public git repository URL containing a plugin.json file.",
    )
    slug = forms.SlugField(
        label="Plugin slug",
        help_text="Unique identifier for this plugin (used as directory name).",
    )
    ref = forms.CharField(
        required=False,
        label="Branch / tag / commit",
        help_text="Leave blank to use the default branch.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", self.FIELD_CLASS)


class WidgetInstanceForm(forms.Form):
    FIELD_CLASS = "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) focus:ring-(--admin-accent)"

    widget_type = forms.ChoiceField(label="Widget type", choices=[])
    area = forms.ChoiceField(label="Area", choices=[])
    order = forms.IntegerField(label="Order", initial=0, min_value=0)
    is_active = forms.BooleanField(label="Active", required=False, initial=True)

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance

        # Populate widget type choices from registry
        from core.plugins import registry
        self.fields["widget_type"].choices = registry.widget_choices()

        # Populate area choices from active theme
        from core.themes import get_active_theme_widget_areas
        areas = get_active_theme_widget_areas()
        self.fields["area"].choices = [(a["slug"], a["label"]) for a in areas]

        # Build config fields based on selected widget type (or first available)
        selected_type = None
        if self.data:
            selected_type = self.data.get("widget_type")
        elif instance and instance.widget_type:
            selected_type = instance.widget_type

        if not selected_type and self.fields["widget_type"].choices:
            selected_type = self.fields["widget_type"].choices[0][0]

        self._config_field_keys = []
        if selected_type:
            cls = registry.get_widget_type(selected_type)
            if cls:
                schema_fields = (cls.config_schema or {}).get("fields", {})
                for key, field_def in schema_fields.items():
                    form_key = f"config_{key}"
                    self._config_field_keys.append(key)
                    field_type = field_def.get("type", "string")
                    label = field_def.get("label", key)
                    default = field_def.get("default", "")
                    initial = ""
                    if instance and isinstance(instance.config, dict):
                        initial = instance.config.get(key, default)
                    elif default:
                        initial = default

                    if field_type in ("text",):
                        self.fields[form_key] = forms.CharField(
                            label=label,
                            required=False,
                            initial=initial,
                            widget=forms.Textarea(attrs={"rows": 5}),
                        )
                    elif field_type == "boolean":
                        self.fields[form_key] = forms.BooleanField(
                            label=label,
                            required=False,
                            initial=bool(initial),
                        )
                    elif field_type == "number":
                        self.fields[form_key] = forms.IntegerField(
                            label=label,
                            required=False,
                            initial=int(initial) if initial else None,
                            min_value=0,
                        )
                    else:
                        self.fields[form_key] = forms.CharField(
                            label=label,
                            required=False,
                            initial=initial,
                        )

        # If editing an existing instance, populate initial values
        if instance:
            self.fields["widget_type"].initial = instance.widget_type
            self.fields["area"].initial = instance.area
            self.fields["order"].initial = instance.order
            self.fields["is_active"].initial = instance.is_active

        for name, field in self.fields.items():
            if name == "is_active":
                continue
            field.widget.attrs.setdefault("class", self.FIELD_CLASS)

    def clean_area(self):
        area = self.cleaned_data.get("area")
        from core.themes import get_active_theme_widget_areas
        areas = get_active_theme_widget_areas()
        valid_slugs = {a["slug"] for a in areas}
        if area not in valid_slugs:
            raise ValidationError(f"'{area}' is not a valid widget area for the active theme.")
        return area

    def save_instance(self):
        """Save to a WidgetInstance, creating or updating as needed."""
        from widgets.models import WidgetInstance

        config = {}
        for key in self._config_field_keys:
            config[key] = self.cleaned_data.get(f"config_{key}")

        if self.instance and self.instance.pk:
            obj = self.instance
        else:
            obj = WidgetInstance()

        obj.widget_type = self.cleaned_data["widget_type"]
        obj.area = self.cleaned_data["area"]
        obj.order = self.cleaned_data["order"]
        obj.is_active = self.cleaned_data.get("is_active", True)
        obj.config = config
        obj.save()
        return obj


_ADMIN_INPUT_CLASS = (
    "mt-1 w-full rounded-2xl border border-(--admin-border) bg-white "
    "px-3 py-2 text-sm shadow-xs focus:border-(--admin-accent) "
    "focus:ring-(--admin-accent)"
)


class ChannelForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        label="Channel name",
        widget=forms.TextInput(attrs={"class": _ADMIN_INPUT_CLASS, "placeholder": "e.g. Blogs"}),
    )


class SubscriptionForm(forms.Form):
    url = forms.URLField(
        max_length=2000,
        label="Feed URL",
        widget=forms.URLInput(attrs={"class": _ADMIN_INPUT_CLASS, "placeholder": "https://example.com/feed.xml"}),
    )
    channel = forms.CharField(widget=forms.HiddenInput())


class OPMLImportForm(forms.Form):
    opml_file = forms.FileField(
        label="OPML file",
        help_text="Upload an OPML file exported from your feed reader.",
        widget=forms.FileInput(attrs={"accept": ".opml,.xml", "class": _ADMIN_INPUT_CLASS}),
    )
