from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.utils import timezone
from django.utils.text import slugify

from blog.models import Post, Tag
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
from core.themes import discover_themes
from core.widgets import CodeMirrorTextarea


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
            ("published", "Published"),
            ("deleted", "Deleted"),
        ],
        label="Status",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
            )


class PostForm(forms.ModelForm):
    tags_text = forms.CharField(
        required=False,
        label="Tags",
        help_text="Comma-separated tags.",
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
    field_order = [
        "title",
        "slug",
        "kind",
        "content",
        "activity_type",
        "tags_text",
        "save_as_draft",
        "published_on",
        "deleted",
        "like_of",
        "repost_of",
        "in_reply_to",
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
        ]
        widgets = {
            "content": CodeMirrorTextarea(),
            "like_of": forms.URLInput(attrs={"placeholder": "https://"}),
            "repost_of": forms.URLInput(attrs={"placeholder": "https://"}),
            "in_reply_to": forms.URLInput(attrs={"placeholder": "https://"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].required = False
        self.fields["slug"].required = False
        self.fields["content"].required = False
        self.fields["activity_type"].required = False
        self.fields["save_as_draft"].initial = not bool(self.instance.published_on)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-[color:var(--admin-border)] text-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )
            if name == "activity_type":
                field.widget.attrs.setdefault("list", "activity-type-list")
                field.widget.attrs.setdefault("placeholder", "Hike")
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
        if self.instance.pk and self.instance.published_on:
            local_time = timezone.localtime(self.instance.published_on)
            self.fields["published_on"].initial = local_time.strftime("%Y-%m-%dT%H:%M")

    def clean_published_on(self):
        value = self.cleaned_data.get("published_on")
        if self.cleaned_data.get("save_as_draft"):
            return None
        if value and timezone.is_naive(value):
            return timezone.make_aware(value)
        return value

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
    q = forms.CharField(required=False, label="Search")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
            )


class PageForm(forms.ModelForm):
    field_order = ["title", "slug", "content", "published_on"]
    published_on = forms.DateTimeField(
        required=True,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Publish time",
    )

    class Meta:
        model = Page
        fields = ["title", "slug", "content", "published_on"]
        widgets = {
            "content": CodeMirrorTextarea(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-[color:var(--admin-border)] text-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
            local_time = timezone.localtime(timezone.now())
            self.fields["published_on"].initial = local_time.strftime(
                "%Y-%m-%dT%H:%M"
            )

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
                    "h-4 w-4 rounded border-[color:var(--admin-border)] text-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
            "favicon",
            "site_author",
            "active_theme",
            "main_menu",
            "footer_menu",
            "robots_txt",
        ]
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
        self.fields["favicon"].queryset = File.objects.filter(kind=File.IMAGE).order_by(
            "-created_at"
        )
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-[color:var(--admin-border)] text-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )
            else:
                field.widget.attrs.setdefault(
                    "class",
                    "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
                )


class ThemeUploadForm(forms.Form):
    archive = forms.FileField(
        help_text="Upload a .zip containing theme.json plus templates/ and static/ folders."
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archive"].widget.attrs.setdefault(
            "class",
            "block w-full text-sm text-[color:var(--admin-ink)] file:mr-4 file:rounded-full file:border-0 file:bg-[color:var(--admin-bg)] file:px-4 file:py-2 file:text-sm file:font-semibold file:text-[color:var(--admin-ink)] hover:file:bg-[color:var(--admin-border)]",
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
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
            )
        self.fields["slug"].widget.attrs.setdefault(
            "data-slug-source", "input[name='git_url']"
        )
        self.fields["slug"].widget.attrs.setdefault("data-slug-source-kind", "url")


class ThemeFileForm(forms.Form):
    theme = forms.ChoiceField(label="Theme")
    path = forms.ChoiceField(label="File")
    content = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 25,
                "class": "w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 font-mono text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )
        self.fields["path"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )


class MenuForm(forms.ModelForm):
    class Meta:
        model = Menu
        fields = ["title"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )


class MenuItemForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = ["text", "url", "weight"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["text"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )
        self.fields["text"].widget.attrs.setdefault("placeholder", "Label")
        self.fields["url"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )
        self.fields["url"].widget.attrs.setdefault("placeholder", "/about or https://...")
        self.fields["weight"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )
        self.fields["from_path"].widget.attrs.setdefault("placeholder", "/old/")
        self.fields["to_path"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )
        self.fields["to_path"].widget.attrs.setdefault("placeholder", "/new/ or https://...")
        self.fields["redirect_type"].widget.attrs.setdefault(
            "class",
            "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
        )


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
            "note": CodeMirrorTextarea(),
            "bday": forms.DateInput(attrs={"type": "date"}),
            "anniversary": forms.DateInput(attrs={"type": "date"}),
            "uid": forms.URLInput(attrs={"placeholder": "https://"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault(
                "class",
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
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
                "mt-1 w-full rounded-2xl border border-[color:var(--admin-border)] bg-white px-3 py-2 text-sm shadow-sm focus:border-[color:var(--admin-accent)] focus:ring-[color:var(--admin-accent)]",
            )
