import json
import logging
import subprocess
from urllib.parse import urlencode, urlparse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from django.contrib import messages
from django.contrib.auth.views import LoginView, LogoutView
from django.core.files.base import ContentFile
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.forms import inlineformset_factory
from django.views.decorators.http import require_http_methods, require_GET, require_POST

from blog.models import Comment, Post
from analytics.models import Visit

from files.models import Attachment, File
from files.gpx import GpxAnonymizeError, GpxAnonymizeOptions, anonymize_gpx

from core.models import (
    HCard,
    HCardEmail,
    HCardPhoto,
    HCardUrl,
    Menu,
    MenuItem,
    Page,
    Redirect,
    SiteConfiguration,
    ThemeInstall,
)
from core.theme_sync import reconcile_installed_themes
from core.themes import (
    ThemeUploadError,
    create_theme_file,
    delete_theme_path,
    discover_themes,
    get_theme,
    ingest_theme_archive,
    install_theme_from_git,
    update_theme_from_git,
    list_theme_directories,
    list_theme_files,
    read_theme_file,
    clear_template_caches,
    resolve_theme_settings,
    save_theme_file,
    sync_themes_from_storage,
    theme_storage_healthcheck,
)
from micropub.models import Webmention
from blog.comments import AkismetError, submit_ham, submit_spam
from micropub.webmention import (
    resend_webmention,
    send_bridgy_publish_webmentions,
    send_webmention,
    send_webmentions_for_post,
)

from .forms import (
    HCardEmailForm,
    HCardForm,
    HCardUrlForm,
    FileForm,
    MenuForm,
    MenuItemForm,
    PageFilterForm,
    PageForm,
    PostFilterForm,
    PostForm,
    RedirectForm,
    SiteConfigurationForm,
    CommentFilterForm,
    ThemeGitInstallForm,
    ThemeFileForm,
    ThemeSettingsForm,
    ThemeUploadForm,
    WebmentionCreateForm,
    WebmentionFilterForm,
)

logger = logging.getLogger(__name__)


def _gpx_form_defaults(request):
    data = request.POST if getattr(request, "POST", None) else {}
    return {
        "gpx_trim_enabled": data.get("gpx_trim", "1") == "1",
        "gpx_trim_distance": data.get("gpx_trim_distance", "500"),
        "gpx_blur_enabled": data.get("gpx_blur", "") == "1",
        "gpx_remove_timestamps": data.get("gpx_remove_timestamps", "") == "1",
    }


def _parse_gpx_anonymize_options(request):
    defaults = _gpx_form_defaults(request)
    errors = []
    trim_distance_m = 500.0
    if defaults["gpx_trim_enabled"]:
        trim_distance = defaults["gpx_trim_distance"]
        try:
            trim_distance_m = float(trim_distance)
        except (TypeError, ValueError):
            trim_distance_m = 500.0
            errors.append("GPX trim distance must be a number of meters.")
        if trim_distance_m < 0:
            trim_distance_m = 500.0
            errors.append("GPX trim distance must be zero or greater.")

    options = GpxAnonymizeOptions(
        trim_enabled=defaults["gpx_trim_enabled"],
        trim_distance_m=trim_distance_m,
        blur_enabled=defaults["gpx_blur_enabled"],
        remove_timestamps=defaults["gpx_remove_timestamps"],
    )
    return options, errors


MenuItemFormSet = inlineformset_factory(
    Menu,
    MenuItem,
    form=MenuItemForm,
    fields=["text", "url", "weight"],
    extra=0,
    can_delete=False,
)
HCardUrlFormSet = inlineformset_factory(
    HCard,
    HCardUrl,
    form=HCardUrlForm,
    fields=["value", "kind"],
    extra=0,
    can_delete=False,
)
HCardEmailFormSet = inlineformset_factory(
    HCard,
    HCardEmail,
    form=HCardEmailForm,
    fields=["value"],
    extra=0,
    can_delete=False,
)

ALLOWED_SUFFIXES = (".html", ".htm", ".txt", ".xml", ".md", ".css", ".js", ".json")


@dataclass
class ThemeFileSelection:
    slug: str
    path: Optional[str]
    content: str = ""


def _parse_positioned_ids(ids, positions):
    meta = {}
    for i in range(min(len(ids), len(positions))):
        try:
            item_id = int(ids[i])
            position = int(positions[i])
        except (TypeError, ValueError):
            continue
        meta[item_id] = {"position": position}
    return meta


def _build_profile_photo_items(
    *,
    request,
    hcard,
    existing_meta=None,
    existing_remove_ids=None,
    uploaded_meta=None,
):
    existing_meta = existing_meta or {}
    existing_remove_ids = existing_remove_ids or set()
    uploaded_meta = uploaded_meta or {}

    photo_items = []
    if hcard and hcard.pk:
        for photo in hcard.photos.select_related("asset"):
            if photo.id in existing_remove_ids:
                continue
            meta = existing_meta.get(photo.id, {})
            photo_items.append(
                {
                    "kind": "existing",
                    "id": photo.id,
                    "url": photo.url,
                    "order": meta.get("position", photo.sort_order),
                }
            )

    if uploaded_meta:
        uploaded_assets = File.objects.filter(
            id__in=uploaded_meta.keys(), owner=request.user
        )
        for asset in uploaded_assets:
            meta = uploaded_meta.get(asset.id, {})
            photo_items.append(
                {
                    "kind": "uploaded",
                    "id": asset.id,
                    "url": asset.file.url,
                    "order": meta.get("position", 0),
                }
            )

    photo_items.sort(key=lambda item: item.get("order", 0))
    return photo_items


def _file_in_use_response(asset):
    message = asset.in_use_message()
    if message:
        return JsonResponse({"error": message}, status=409)
    return None


def _file_usage_items(asset, *, request=None):
    items = []
    attachments = Attachment.objects.filter(asset=asset).select_related("content_type")
    for attachment in attachments:
        content_object = attachment.content_object
        label = attachment.content_type.name or "Attachment"
        detail = str(content_object) if content_object else "Unknown item"
        url = None
        if isinstance(content_object, Post):
            label = "Post"
            detail = content_object.title or content_object.slug
            url = reverse("site_admin:post_edit", kwargs={"slug": content_object.slug})
        elif isinstance(content_object, Page):
            label = "Page"
            detail = content_object.title or content_object.slug
            url = reverse("site_admin:page_edit", kwargs={"slug": content_object.slug})
        if attachment.role:
            detail = f"{detail} (role: {attachment.role})"
        items.append({"label": label, "detail": detail, "url": url})

    for photo in asset.hcard_photos.select_related("hcard__user"):
        hcard = photo.hcard
        name = hcard.name or (hcard.user.username if hcard.user_id else "Profile")
        url = None
        if request and hcard.user_id == request.user.id:
            url = reverse("site_admin:profile_edit")
        items.append({"label": "Profile photo", "detail": name, "url": url})

    return items


def _sync_profile_photos(
    *,
    request,
    hcard,
    existing_meta,
    existing_remove_ids,
    uploaded_meta,
):
    if existing_remove_ids:
        removed = HCardPhoto.objects.filter(
            hcard=hcard, id__in=existing_remove_ids
        ).select_related("asset")
        for photo in removed:
            asset = photo.asset if photo.asset_id else None
            photo.delete()
            if asset and asset.owner_id == request.user.id:
                still_used = HCardPhoto.objects.filter(asset_id=asset.id).exists()
                if not still_used and not asset.attachments.exists():
                    asset.delete()

    if existing_meta:
        for photo in HCardPhoto.objects.filter(
            hcard=hcard, id__in=existing_meta.keys()
        ):
            new_order = existing_meta.get(photo.id, {}).get(
                "position", photo.sort_order
            )
            if photo.sort_order != new_order:
                photo.sort_order = new_order
                photo.save(update_fields=["sort_order"])

    if uploaded_meta:
        assets = File.objects.filter(
            id__in=uploaded_meta.keys(), owner=request.user
        )
        for asset in assets:
            meta = uploaded_meta.get(asset.id, {})
            HCardPhoto.objects.create(
                hcard=hcard,
                asset=asset,
                value=asset.file.url,
                sort_order=meta.get("position", 0),
            )


def _staff_guard(request):
    if not request.user.is_authenticated:
        login_url = reverse("site_admin:login")
        query = urlencode({"next": request.get_full_path()})
        return redirect(f"{login_url}?{query}")
    if not request.user.is_staff:
        return HttpResponseForbidden()
    return None


class SiteAdminLoginView(LoginView):
    template_name = "site_admin/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        if not user.is_staff:
            form.add_error(None, "You do not have access to the site admin.")
            return self.form_invalid(form)
        return super().form_valid(form)


class SiteAdminLogoutView(LogoutView):
    next_page = "site_admin:login"


def _theme_choices():
    return [(theme.slug, theme.label) for theme in discover_themes()]


def _is_git_path(path):
    if not path:
        return False
    return ".git" in path.split("/")


def _build_theme_selection(request, slug_param):
    themes = discover_themes()
    default_slug = slug_param or request.GET.get("theme") or (themes[0].slug if themes else "")
    selected_slug = request.POST.get("theme", default_slug)

    files = list_theme_files(selected_slug, suffixes=ALLOWED_SUFFIXES) if selected_slug else []
    files = [path for path in files if not _is_git_path(path)]
    default_path = request.GET.get("path") or (files[0] if files else None)
    if _is_git_path(default_path):
        default_path = None
    selected_path = request.POST.get("path") or default_path
    if _is_git_path(selected_path):
        selected_path = None

    content = ""
    if selected_slug and selected_path:
        try:
            content = read_theme_file(selected_slug, selected_path)
        except ThemeUploadError as exc:
            messages.error(request, str(exc))
        except UnicodeDecodeError:
            messages.error(request, "That file cannot be edited as text.")

    return ThemeFileSelection(slug=selected_slug or "", path=selected_path, content=content)


@require_http_methods(["GET"])
def admin_bar(request):
    if not request.user.is_authenticated:
        return HttpResponse(status=204)
    if not request.user.is_staff:
        return HttpResponseForbidden()

    return render(request, "site_admin/_admin_bar.html")


def _strip_page_query(request):
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return query_params.urlencode()


def _is_local_url(url, request):
    if not url:
        return False
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    return parsed.netloc == request.get_host()


def _build_daily_counts(qs, start_date, end_date):
    day_rows = (
        qs.annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(count=Count("id"))
    )
    day_map = {row["day"]: row["count"] for row in day_rows}
    labels = []
    counts = []
    current = start_date
    while current <= end_date:
        labels.append(current.strftime("%b %d"))
        counts.append(day_map.get(current, 0))
        current += timedelta(days=1)
    return labels, counts


def _build_daily_unique_sessions(qs, start_date, end_date):
    day_rows = (
        qs.annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(count=Count("session_key", distinct=True))
    )
    day_map = {row["day"]: row["count"] for row in day_rows}
    labels = []
    counts = []
    current = start_date
    while current <= end_date:
        labels.append(current.strftime("%b %d"))
        counts.append(day_map.get(current, 0))
        current += timedelta(days=1)
    return labels, counts


def _redirect_path_suggestions():
    pages = Page.objects.values_list("slug", flat=True)
    posts = Post.objects.filter(deleted=False, published_on__isnull=False).values_list(
        "slug", flat=True
    )
    menu_paths = MenuItem.objects.values_list("url", flat=True)
    suggestions = {
        "/",
        *[reverse("page", kwargs={"slug": slug}) for slug in pages],
        *[reverse("post", kwargs={"slug": slug}) for slug in posts],
    }
    suggestions.update(path for path in menu_paths if path and path.startswith("/"))
    return sorted(suggestions)


def dashboard(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    recent_posts = Post.objects.order_by("-published_on", "-id")[:5]
    summary_days = 7
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=summary_days - 1)
    analytics_qs = (
        Visit.objects.filter(
            started_at__date__gte=start_date, started_at__date__lte=end_date
        )
        .exclude(path__startswith="/admin")
        .exclude(path__startswith="/analytics")
    )
    analytics_stats = analytics_qs.aggregate(
        total_page_views=Count("id"),
        unique_sessions=Count("session_key", distinct=True),
        unique_users=Count("user", distinct=True),
        avg_duration=Avg("duration_seconds"),
    )
    analytics_labels, analytics_counts = _build_daily_counts(
        analytics_qs, start_date, end_date
    )
    top_paths = (
        analytics_qs.values("path")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    return render(
        request,
        "site_admin/dashboard.html",
        {
            "recent_posts": recent_posts,
            "analytics_stats": analytics_stats,
            "analytics_labels": analytics_labels,
            "analytics_counts": analytics_counts,
            "analytics_top_paths": top_paths,
        },
    )


def analytics_dashboard(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=29)
    start_param = request.GET.get("start")
    end_param = request.GET.get("end")
    if start_param:
        try:
            start_date = date.fromisoformat(start_param)
        except ValueError:
            start_date = start_date
    if end_param:
        try:
            end_date = date.fromisoformat(end_param)
        except ValueError:
            end_date = end_date
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    window_days = (end_date - start_date).days + 1
    today = timezone.localdate()
    month_start = today.replace(day=1)
    presets = {
        "last_7": {
            "label": "Last 7 days",
            "start": (today - timedelta(days=6)).isoformat(),
            "end": today.isoformat(),
        },
        "last_30": {
            "label": "Last 30 days",
            "start": (today - timedelta(days=29)).isoformat(),
            "end": today.isoformat(),
        },
        "last_90": {
            "label": "Last 90 days",
            "start": (today - timedelta(days=89)).isoformat(),
            "end": today.isoformat(),
        },
        "this_month": {
            "label": "This month",
            "start": month_start.isoformat(),
            "end": today.isoformat(),
        },
    }
    qs = (
        Visit.objects.filter(
            started_at__date__gte=start_date, started_at__date__lte=end_date
        )
        .exclude(path__startswith="/admin")
        .exclude(path__startswith="/analytics")
    )
    stats = qs.aggregate(
        total_page_views=Count("id"),
        unique_sessions=Count("session_key", distinct=True),
        unique_users=Count("user", distinct=True),
        unique_ips=Count("ip_address", distinct=True),
        avg_duration=Avg("duration_seconds"),
    )
    daily_labels, daily_views = _build_daily_counts(qs, start_date, end_date)
    _, daily_sessions = _build_daily_unique_sessions(qs, start_date, end_date)

    top_paths = list(
        qs.values("path")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    top_referrers = list(
        qs.exclude(referrer="")
        .values("referrer")
        .annotate(count=Count("id"))
        .order_by("-count")[:8]
    )
    countries = list(
        qs.exclude(country="")
        .values("country")
        .annotate(count=Count("id"))
        .order_by("-count")[:8]
    )
    error_visits = list(
        qs.filter(response_status_code__gte=400)
        .values("path", "response_status_code")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    return render(
        request,
        "site_admin/analytics/index.html",
        {
            "stats": stats,
            "daily_labels": daily_labels,
            "daily_views": daily_views,
            "daily_sessions": daily_sessions,
            "top_paths": top_paths,
            "top_referrers": top_referrers,
            "countries": countries,
            "error_visits": error_visits,
            "window_days": window_days,
            "start_date": start_date,
            "end_date": end_date,
            "presets": presets,
        },
    )


@require_http_methods(["GET"])
def menu_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    settings_obj = SiteConfiguration.get_solo()
    menus = Menu.objects.order_by("title").prefetch_related("menuitem_set")
    return render(
        request,
        "site_admin/menus/index.html",
        {
            "menus": menus,
            "main_menu_id": settings_obj.main_menu_id,
            "footer_menu_id": settings_obj.footer_menu_id,
        },
    )


@require_http_methods(["GET", "POST"])
def menu_edit(request, menu_id=None):
    guard = _staff_guard(request)
    if guard:
        return guard

    menu = None
    if menu_id is not None:
        menu = get_object_or_404(Menu, pk=menu_id)

    saved = False
    if request.method == "POST":
        form = MenuForm(request.POST, instance=menu)
        formset = MenuItemFormSet(request.POST, instance=menu, prefix="items")
        if form.is_valid() and formset.is_valid():
            menu = form.save()
            formset.instance = menu
            formset.save()
            saved = True
    else:
        form = MenuForm(instance=menu)
        formset = MenuItemFormSet(instance=menu, prefix="items")

    return render(
        request,
        "site_admin/menus/edit.html",
        {
            "form": form,
            "formset": formset,
            "menu": menu,
            "saved": saved,
        },
    )


@require_POST
def menu_item_delete(request, item_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    item = get_object_or_404(MenuItem, pk=item_id)
    item.delete()
    return HttpResponse("")


@require_http_methods(["GET"])
def redirect_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    redirects = Redirect.objects.order_by("from_path", "id")
    return render(
        request,
        "site_admin/redirects/index.html",
        {
            "redirects": redirects,
        },
    )


@require_http_methods(["GET", "POST"])
def redirect_edit(request, redirect_id=None):
    guard = _staff_guard(request)
    if guard:
        return guard

    redirect_obj = None
    if redirect_id is not None:
        redirect_obj = get_object_or_404(Redirect, pk=redirect_id)

    saved = False
    initial = {}
    if redirect_obj is None:
        from_path = request.GET.get("from")
        to_path = request.GET.get("to")
        if from_path:
            initial["from_path"] = from_path
        if to_path:
            initial["to_path"] = to_path
    if request.method == "POST":
        form = RedirectForm(request.POST, instance=redirect_obj)
        if form.is_valid():
            redirect_obj = form.save()
            saved = True
    else:
        form = RedirectForm(instance=redirect_obj, initial=initial)
    form.fields["to_path"].widget.attrs["list"] = "redirect-path-options"
    path_suggestions = _redirect_path_suggestions()

    return render(
        request,
        "site_admin/redirects/edit.html",
        {
            "form": form,
            "redirect_obj": redirect_obj,
            "saved": saved,
            "path_suggestions": path_suggestions,
        },
    )


@require_POST
def redirect_delete(request, redirect_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    redirect_obj = get_object_or_404(Redirect, pk=redirect_id)
    redirect_obj.delete()
    return redirect("site_admin:redirect_list")


def _filtered_pages(request):
    form = PageFilterForm(request.GET or None)
    pages = Page.objects.order_by("-published_on", "-id")
    if form.is_valid():
        query = form.cleaned_data.get("q")
        if query:
            pages = pages.filter(Q(title__icontains=query) | Q(slug__icontains=query))
    return form, pages


@require_http_methods(["GET"])
def page_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    filter_form, pages = _filtered_pages(request)
    paginator = Paginator(pages, 20)
    page_number = request.GET.get("page")
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "paginator": paginator,
        "base_query": _strip_page_query(request),
    }

    if request.headers.get("HX-Request"):
        return render(request, "site_admin/pages/_list.html", context)

    return render(request, "site_admin/pages/index.html", context)


@require_http_methods(["GET", "POST"])
def page_edit(request, slug=None):
    guard = _staff_guard(request)
    if guard:
        return guard

    page = None
    if slug:
        page = get_object_or_404(Page, slug=slug)
    is_new = page is None

    if request.method == "POST":
        form = PageForm(request.POST, instance=page)
        if form.is_valid():
            saved_page = form.save(commit=False)
            if not saved_page.author_id:
                saved_page.author = request.user
            saved_page.save()
            if request.headers.get("HX-Request"):
                if is_new:
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = reverse(
                        "site_admin:page_edit", kwargs={"slug": saved_page.slug}
                    )
                    return response
                refreshed_form = PageForm(instance=saved_page)
                return render(
                    request,
                    "site_admin/pages/_form_messages.html",
                    {"form": refreshed_form, "page": saved_page, "saved": True},
                )
            return redirect("site_admin:page_edit", slug=saved_page.slug)
        template_name = (
            "site_admin/pages/_form_messages.html"
            if request.headers.get("HX-Request")
            else "site_admin/pages/edit.html"
        )
        return render(
            request,
            template_name,
            {"form": form, "page": page, "saved": False},
        )

    form = PageForm(instance=page)
    template_name = (
        "site_admin/pages/_form.html"
        if request.headers.get("HX-Request")
        else "site_admin/pages/edit.html"
    )
    return render(
        request,
        template_name,
        {"form": form, "page": page, "saved": False},
    )


@require_POST
def page_delete(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    page = get_object_or_404(Page, slug=slug)
    page.delete()
    return redirect("site_admin:page_list")


def _filtered_posts(request):
    form = PostFilterForm(request.GET or None)
    posts = Post.objects.order_by("-published_on", "-id")
    if form.is_valid():
        query = form.cleaned_data.get("q")
        kind = form.cleaned_data.get("kind")
        status = form.cleaned_data.get("status")
        if query:
            posts = posts.filter(Q(title__icontains=query) | Q(slug__icontains=query))
        if kind:
            posts = posts.filter(kind=kind)
        if status == "draft":
            posts = posts.filter(published_on__isnull=True, deleted=False)
        elif status == "published":
            posts = posts.filter(published_on__isnull=False, deleted=False)
        elif status == "deleted":
            posts = posts.filter(deleted=True)
    return form, posts


@require_http_methods(["GET"])
def post_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    filter_form, posts = _filtered_posts(request)
    paginator = Paginator(posts, 20)
    page_number = request.GET.get("page")
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "paginator": paginator,
        "base_query": _strip_page_query(request),
    }

    if request.headers.get("HX-Request"):
        return render(request, "site_admin/posts/_list.html", context)

    return render(request, "site_admin/posts/index.html", context)


def _filtered_webmentions(request):
    form = WebmentionFilterForm(request.GET or None)
    webmentions = Webmention.objects.select_related("target_post").order_by("-created_at", "-id")
    if form.is_valid():
        query = form.cleaned_data.get("q")
        status = form.cleaned_data.get("status")
        mention_type = form.cleaned_data.get("mention_type")
        if query:
            webmentions = webmentions.filter(
                Q(source__icontains=query) | Q(target__icontains=query)
            )
        if status:
            webmentions = webmentions.filter(status=status)
        if mention_type:
            webmentions = webmentions.filter(mention_type=mention_type)
    return form, webmentions


def _akismet_payload_for_comment(comment, request):
    return {
        "blog": request.build_absolute_uri("/"),
        "user_ip": comment.ip_address or "",
        "user_agent": comment.user_agent or "",
        "referrer": comment.referrer or "",
        "permalink": request.build_absolute_uri(comment.post.get_absolute_url()),
        "comment_type": "comment",
        "comment_author": comment.author_name,
        "comment_author_email": comment.author_email or "",
        "comment_author_url": comment.author_url or "",
        "comment_content": comment.content,
        "comment_date_gmt": comment.created_at.isoformat(),
    }


def _filtered_comments(request):
    form = CommentFilterForm(request.GET or None)
    comments = Comment.objects.select_related("post").order_by("-created_at", "-id")
    if form.is_valid():
        query = form.cleaned_data.get("q")
        status = form.cleaned_data.get("status")
        post = form.cleaned_data.get("post")
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")
        if query:
            comments = comments.filter(
                Q(author_name__icontains=query)
                | Q(author_email__icontains=query)
                | Q(author_url__icontains=query)
                | Q(content__icontains=query)
            )
        if status:
            comments = comments.filter(status=status)
        if post:
            comments = comments.filter(post=post)
        if start_date:
            comments = comments.filter(created_at__date__gte=start_date)
        if end_date:
            comments = comments.filter(created_at__date__lte=end_date)
    return form, comments


@require_http_methods(["GET"])
def webmention_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    filter_form, webmentions = _filtered_webmentions(request)
    paginator = Paginator(webmentions, 20)
    page_number = request.GET.get("page")
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "paginator": paginator,
        "base_query": _strip_page_query(request),
        "endpoint_url": request.build_absolute_uri(reverse("webmention-endpoint")),
    }

    if request.headers.get("HX-Request"):
        return render(request, "site_admin/webmentions/_list.html", context)

    return render(request, "site_admin/webmentions/index.html", context)


@require_http_methods(["GET", "POST"])
def webmention_create(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    if request.method == "POST":
        form = WebmentionCreateForm(request.POST)
        if form.is_valid():
            logger.info(
                "Webmention send requested",
                extra={
                    "webmention_source": form.cleaned_data["source"],
                    "webmention_target": form.cleaned_data["target"],
                    "webmention_type": form.cleaned_data["mention_type"],
                },
            )
            try:
                mention = send_webmention(
                    form.cleaned_data["source"],
                    form.cleaned_data["target"],
                    mention_type=form.cleaned_data["mention_type"],
                )
            except Exception:
                logger.exception(
                    "Webmention send crashed",
                    extra={
                        "webmention_source": form.cleaned_data["source"],
                        "webmention_target": form.cleaned_data["target"],
                        "webmention_type": form.cleaned_data["mention_type"],
                    },
                )
                raise
            if mention.status == Webmention.ACCEPTED:
                messages.success(request, "Webmention sent successfully.")
            elif mention.status == Webmention.PENDING:
                messages.warning(
                    request,
                    "Webmention accepted for processing and is still pending.",
                )
            elif mention.status == Webmention.TIMED_OUT:
                messages.warning(
                    request,
                    "Webmention request timed out. You can retry it later.",
                )
            else:
                messages.error(
                    request,
                    "Webmention was rejected by the target endpoint.",
                )
            return redirect("site_admin:webmention_detail", mention_id=mention.id)
        logger.info(
            "Webmention form invalid",
            extra={
                "webmention_errors": form.errors.as_json(),
                "webmention_post": request.POST.dict(),
            },
        )
    else:
        form = WebmentionCreateForm()

    return render(
        request,
        "site_admin/webmentions/new.html",
        {
            "form": form,
            "endpoint_url": request.build_absolute_uri(reverse("webmention-endpoint")),
        },
    )


@require_http_methods(["GET"])
def webmention_detail(request, mention_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    mention = get_object_or_404(Webmention, pk=mention_id)
    can_resend = mention.status in (Webmention.PENDING, Webmention.TIMED_OUT) and _is_local_url(
        mention.source,
        request,
    )
    can_delete = mention.status == Webmention.REJECTED
    can_moderate = mention.status == Webmention.PENDING and _is_local_url(mention.target, request)
    return render(
        request,
        "site_admin/webmentions/detail.html",
        {
            "mention": mention,
            "can_resend": can_resend,
            "can_delete": can_delete,
            "can_moderate": can_moderate,
        },
    )


@require_POST
def webmention_resend(request, mention_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    mention = get_object_or_404(Webmention, pk=mention_id)
    if mention.status not in (Webmention.PENDING, Webmention.TIMED_OUT):
        messages.error(request, "Only pending or timed-out webmentions can be resent.")
        return redirect("site_admin:webmention_detail", mention_id=mention.id)
    if not _is_local_url(mention.source, request):
        messages.error(request, "Only webmentions sourced from this site can be resent.")
        return redirect("site_admin:webmention_detail", mention_id=mention.id)

    resend_webmention(mention)
    if mention.status == Webmention.ACCEPTED:
        messages.success(request, "Webmention resent successfully.")
    elif mention.status == Webmention.PENDING:
        messages.warning(request, "Webmention is still pending after resend.")
    elif mention.status == Webmention.TIMED_OUT:
        messages.warning(request, "Webmention resend timed out.")
    else:
        messages.error(request, "Webmention resend was rejected.")
    return redirect("site_admin:webmention_detail", mention_id=mention.id)


@require_POST
def webmention_delete(request, mention_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    mention = get_object_or_404(Webmention, pk=mention_id)
    if mention.status != Webmention.REJECTED:
        messages.error(request, "Only rejected webmentions can be deleted.")
        return redirect("site_admin:webmention_detail", mention_id=mention.id)
    mention.delete()
    messages.success(request, "Webmention deleted.")
    return redirect("site_admin:webmention_list")


@require_POST
def webmention_approve(request, mention_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    mention = get_object_or_404(Webmention, pk=mention_id)
    if mention.status != Webmention.PENDING:
        messages.error(request, "Only pending webmentions can be approved.")
        return redirect("site_admin:webmention_detail", mention_id=mention.id)
    mention.status = Webmention.ACCEPTED
    mention.error = ""
    mention.save(update_fields=["status", "error", "updated_at"])
    messages.success(request, "Webmention approved.")
    return redirect("site_admin:webmention_detail", mention_id=mention.id)


@require_POST
def webmention_reject(request, mention_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    mention = get_object_or_404(Webmention, pk=mention_id)
    if mention.status != Webmention.PENDING:
        messages.error(request, "Only pending webmentions can be rejected.")
        return redirect("site_admin:webmention_detail", mention_id=mention.id)
    mention.status = Webmention.REJECTED
    if not mention.error:
        mention.error = "Rejected by admin"
    mention.save(update_fields=["status", "error", "updated_at"])
    messages.success(request, "Webmention rejected.")
    return redirect("site_admin:webmention_detail", mention_id=mention.id)


@require_http_methods(["GET"])
def comment_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    filter_form, comments = _filtered_comments(request)
    paginator = Paginator(comments, 20)
    page_number = request.GET.get("page")
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "paginator": paginator,
        "base_query": _strip_page_query(request),
    }

    if request.headers.get("HX-Request"):
        return render(request, "site_admin/comments/_list.html", context)

    return render(request, "site_admin/comments/index.html", context)


@require_http_methods(["GET"])
def comment_detail(request, comment_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    comment = get_object_or_404(Comment.objects.select_related("post"), pk=comment_id)
    can_approve = comment.status in (Comment.PENDING, Comment.SPAM, Comment.REJECTED)
    can_spam = comment.status in (Comment.PENDING, Comment.APPROVED, Comment.REJECTED)
    can_delete = comment.status != Comment.DELETED
    return render(
        request,
        "site_admin/comments/detail.html",
        {
            "comment": comment,
            "can_approve": can_approve,
            "can_spam": can_spam,
            "can_delete": can_delete,
        },
    )


@require_POST
def comment_approve(request, comment_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    comment = get_object_or_404(Comment, pk=comment_id)
    if comment.status == Comment.DELETED:
        messages.error(request, "Deleted comments cannot be approved.")
        return redirect("site_admin:comment_detail", comment_id=comment.id)

    comment.status = Comment.APPROVED
    comment.akismet_classification = "ham"
    comment.save(update_fields=["status", "akismet_classification"])
    try:
        submit_ham(_akismet_payload_for_comment(comment, request))
    except AkismetError:
        messages.warning(request, "Comment approved, but Akismet could not be notified.")
    else:
        messages.success(request, "Comment approved.")
    return redirect("site_admin:comment_detail", comment_id=comment.id)


@require_POST
def comment_mark_spam(request, comment_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    comment = get_object_or_404(Comment, pk=comment_id)
    if comment.status == Comment.DELETED:
        messages.error(request, "Deleted comments cannot be marked as spam.")
        return redirect("site_admin:comment_detail", comment_id=comment.id)

    comment.status = Comment.SPAM
    comment.akismet_classification = "spam"
    comment.save(update_fields=["status", "akismet_classification"])
    try:
        submit_spam(_akismet_payload_for_comment(comment, request))
    except AkismetError:
        messages.warning(request, "Comment marked as spam, but Akismet could not be notified.")
    else:
        messages.success(request, "Comment marked as spam.")
    return redirect("site_admin:comment_detail", comment_id=comment.id)


@require_POST
def comment_delete(request, comment_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    comment = get_object_or_404(Comment, pk=comment_id)
    if comment.status == Comment.DELETED:
        messages.error(request, "Comment is already deleted.")
        return redirect("site_admin:comment_detail", comment_id=comment.id)

    comment.status = Comment.DELETED
    comment.akismet_classification = "deleted"
    comment.save(update_fields=["status", "akismet_classification"])
    messages.success(request, "Comment deleted.")
    return redirect("site_admin:comment_list")


@require_http_methods(["GET"])
def file_list(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    files = File.objects.order_by("-created_at", "-id")
    paginator = Paginator(files, 24)
    page_number = request.GET.get("page")
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(
        request,
        "site_admin/files/index.html",
        {
            "page_obj": page_obj,
            "paginator": paginator,
            "base_query": _strip_page_query(request),
        },
    )


@require_http_methods(["GET", "POST"])
def file_create(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    saved = False
    if request.method == "POST":
        form = FileForm(request.POST, request.FILES)
        if form.is_valid():
            asset = form.save(commit=False)
            if not asset.owner_id:
                asset.owner = request.user
            asset.save()
            form = FileForm(instance=asset)
            saved = True
    else:
        form = FileForm(initial={"owner": request.user})

    return render(
        request,
        "site_admin/files/new.html",
        {
            "form": form,
            "saved": saved,
        },
    )


@require_http_methods(["GET", "POST"])
def file_edit(request, file_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    asset = get_object_or_404(File, pk=file_id)
    saved = False

    if request.method == "POST":
        form = FileForm(request.POST, request.FILES, instance=asset)
        if form.is_valid():
            form.save()
            saved = True
    else:
        form = FileForm(instance=asset)

    return render(
        request,
        "site_admin/files/edit.html",
        {
            "form": form,
            "asset": asset,
            "saved": saved,
        },
    )


@require_http_methods(["GET", "POST"])
def file_delete(request, file_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    asset = get_object_or_404(File, pk=file_id)
    in_use_message = asset.in_use_message()
    can_delete = not in_use_message
    usage_items = _file_usage_items(asset, request=request)

    if request.method == "POST":
        if not can_delete:
            return render(
                request,
                "site_admin/files/delete.html",
                {
                    "asset": asset,
                    "can_delete": can_delete,
                    "in_use_message": in_use_message,
                    "usage_items": usage_items,
                },
                status=409,
            )
        asset.delete()
        return redirect("site_admin:file_list")

    return render(
        request,
        "site_admin/files/delete.html",
        {
            "asset": asset,
            "can_delete": can_delete,
            "in_use_message": in_use_message,
            "usage_items": usage_items,
        },
    )


@require_http_methods(["GET", "POST"])
def post_edit(request, slug=None):
    guard = _staff_guard(request)
    if guard:
        return guard

    post = None
    if slug:
        post = get_object_or_404(Post, slug=slug)
    is_new = post is None
    was_published = post.is_published() if post else False

    if request.method == "POST":
        form = PostForm(request.POST, instance=post)
        existing_ids = request.POST.getlist("existing_ids")
        existing_alts = request.POST.getlist("existing_alts")
        existing_captions = request.POST.getlist("existing_captions")
        existing_positions = request.POST.getlist("existing_positions")
        existing_remove_ids = set()
        for raw_id in request.POST.getlist("existing_remove_ids"):
            try:
                existing_remove_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        existing_meta = {}
        for i in range(min(len(existing_ids), len(existing_positions))):
            try:
                asset_id = int(existing_ids[i])
                position = int(existing_positions[i])
            except (TypeError, ValueError):
                continue
            alt_text = existing_alts[i] if i < len(existing_alts) else ""
            caption = existing_captions[i] if i < len(existing_captions) else ""
            existing_meta[asset_id] = {
                "position": position,
                "alt": alt_text,
                "caption": caption,
            }

        uploaded_ids = request.POST.getlist("uploaded_ids")
        uploaded_alts = request.POST.getlist("uploaded_alts")
        uploaded_captions = request.POST.getlist("uploaded_captions")
        uploaded_positions = request.POST.getlist("uploaded_positions")
        uploaded_meta = {}
        for i in range(min(len(uploaded_ids), len(uploaded_positions))):
            try:
                asset_id = int(uploaded_ids[i])
                position = int(uploaded_positions[i])
            except (TypeError, ValueError):
                continue
            alt_text = uploaded_alts[i] if i < len(uploaded_alts) else ""
            caption = uploaded_captions[i] if i < len(uploaded_captions) else ""
            uploaded_meta[asset_id] = {
                "position": position,
                "alt": alt_text,
                "caption": caption,
            }

        uploads = request.FILES.getlist("photos")
        gpx_upload = request.FILES.get("gpx_file")
        gpx_remove = request.POST.get("gpx_remove") == "1"
        gpx_options, gpx_option_errors = _parse_gpx_anonymize_options(request)
        if form.is_valid():
            selected_kind = form.cleaned_data.get("kind")
            content_value = (form.cleaned_data.get("content") or "").strip()
            like_of_value = form.cleaned_data.get("like_of") or ""
            repost_of_value = form.cleaned_data.get("repost_of") or ""
            in_reply_to_value = form.cleaned_data.get("in_reply_to") or ""
            activity_type = (form.cleaned_data.get("activity_type") or "").strip()

            errors = []
            if gpx_upload and Path(gpx_upload.name).suffix.lower() != ".gpx":
                errors.append("GPX uploads must use a .gpx file.")
            if gpx_upload:
                errors.extend(gpx_option_errors)
            if selected_kind == Post.LIKE and not like_of_value:
                errors.append("Provide a URL for the like.")
            if selected_kind == Post.REPOST and not repost_of_value:
                errors.append("Provide a URL for the repost.")
            if selected_kind == Post.REPLY and not in_reply_to_value:
                errors.append("Provide a URL for the reply.")
            if selected_kind in (Post.ARTICLE, Post.NOTE) and not content_value:
                errors.append("Content is required for this post type.")
            existing_gpx = (
                post.attachments.filter(role="gpx").exists()
                if post
                else False
            )
            has_gpx = (existing_gpx and not gpx_remove) or bool(gpx_upload)
            if selected_kind == Post.ACTIVITY:
                if not activity_type:
                    errors.append("Add an activity type (e.g., hike, bike ride).")
                if not has_gpx:
                    errors.append("Add a GPX file for activity posts.")
            remaining_existing_photos = (
                post.attachments.filter(role="photo")
                .exclude(asset__id__in=existing_remove_ids)
                .exists()
                if post
                else False
            )
            has_new_uploads = bool(uploaded_meta) or bool(uploads)
            if selected_kind == Post.PHOTO and not (
                content_value or has_new_uploads or remaining_existing_photos
            ):
                errors.append("Add a caption or at least one photo for photo posts.")

            if errors:
                for error in errors:
                    form.add_error(None, error)
                context = _build_post_form_context(
                    request=request,
                    form=form,
                    post=post,
                    saved=False,
                    existing_meta=existing_meta,
                    existing_remove_ids=existing_remove_ids,
                    uploaded_meta=uploaded_meta,
                )
                template_name = (
                    "site_admin/posts/_form_messages.html"
                    if request.headers.get("HX-Request")
                    else "site_admin/posts/edit.html"
                )
                return render(request, template_name, context)

            saved_post = form.save(commit=False)
            if not saved_post.author_id:
                saved_post.author = request.user
            if (
                is_new
                and not saved_post.published_on
                and not form.cleaned_data.get("save_as_draft")
            ):
                saved_post.published_on = timezone.now()
            if not content_value:
                if selected_kind == Post.LIKE:
                    content_value = f"Liked {like_of_value}"
                elif selected_kind == Post.REPOST:
                    content_value = f"Reposted {repost_of_value}"
                elif selected_kind == Post.REPLY:
                    content_value = f"Reply to {in_reply_to_value}"
            saved_post.content = content_value
            saved_post.save()
            form.save_tags(saved_post)

            if post:
                for attachment in list(
                    saved_post.attachments.select_related("asset")
                ):
                    asset = attachment.asset
                    asset_id = asset.id
                    if asset_id in existing_remove_ids:
                        attachment.delete()
                        if not asset.is_in_use():
                            asset.delete()
                        continue
                    meta = existing_meta.get(asset_id)
                    if not meta:
                        continue
                    asset.alt_text = meta.get("alt", "")
                    asset.caption = meta.get("caption", "")
                    asset.save(update_fields=["alt_text", "caption"])
                    attachment.sort_order = meta.get(
                        "position", attachment.sort_order
                    )
                    attachment.save(update_fields=["sort_order"])

            if uploaded_meta:
                uploaded_assets = File.objects.filter(
                    id__in=uploaded_meta.keys(), owner=request.user
                )
                for asset in uploaded_assets:
                    meta = uploaded_meta.get(asset.id, {})
                    asset.alt_text = meta.get("alt", "")
                    asset.caption = meta.get("caption", "")
                    asset.save(update_fields=["alt_text", "caption"])
                    Attachment.objects.create(
                        content_object=saved_post,
                        asset=asset,
                        role="photo",
                        sort_order=meta.get("position", 0),
                    )

            for index, upload in enumerate(uploads):
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=request.user,
                )
                Attachment.objects.create(
                    content_object=saved_post,
                    asset=asset,
                    role="photo",
                    sort_order=index,
                )
            existing_gpx_attachments = list(
                saved_post.attachments.select_related("asset").filter(role="gpx")
            )
            if gpx_remove or gpx_upload:
                for attachment in existing_gpx_attachments:
                    asset = attachment.asset
                    attachment.delete()
                    if asset and not asset.is_in_use():
                        asset.delete()
                existing_gpx_attachments = []

            if gpx_upload:
                try:
                    anonymized_gpx = anonymize_gpx(
                        gpx_upload.read(), gpx_options
                    )
                except GpxAnonymizeError as exc:
                    form.add_error(None, str(exc))
                    context = _build_post_form_context(
                        request=request,
                        form=form,
                        post=post,
                        saved=False,
                        existing_meta=existing_meta,
                        existing_remove_ids=existing_remove_ids,
                        uploaded_meta=uploaded_meta,
                    )
                    template_name = (
                        "site_admin/posts/_form_messages.html"
                        if request.headers.get("HX-Request")
                        else "site_admin/posts/edit.html"
                    )
                    return render(request, template_name, context)

                anonymized_upload = ContentFile(
                    anonymized_gpx, name=gpx_upload.name
                )
                asset = File.objects.create(
                    kind=File.DOC,
                    file=anonymized_upload,
                    owner=request.user,
                )
                attachment = Attachment.objects.create(
                    content_object=saved_post,
                    asset=asset,
                    role="gpx",
                )
                existing_gpx_attachments = [attachment]

            mf2_payload = saved_post.mf2 if isinstance(saved_post.mf2, dict) else {}
            if selected_kind == Post.ACTIVITY:
                activity_props = {}
                if activity_type:
                    activity_props["activity-type"] = [activity_type]
                    activity_props["name"] = [activity_type]
                if existing_gpx_attachments:
                    track_url = existing_gpx_attachments[0].asset.file.url
                    activity_props["track"] = [track_url]
                if activity_props:
                    mf2_payload["activity"] = [
                        {"type": ["h-activity"], "properties": activity_props}
                    ]
            else:
                mf2_payload.pop("activity", None)

            saved_post.mf2 = mf2_payload
            saved_post.save(update_fields=["mf2"])
            source_url = request.build_absolute_uri(saved_post.get_absolute_url())
            send_webmentions_for_post(saved_post, source_url)
            if saved_post.published_on and (is_new or not was_published):
                send_bridgy_publish_webmentions(
                    saved_post,
                    source_url,
                    SiteConfiguration.get_solo(),
                )
            if request.headers.get("HX-Request"):
                if is_new:
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = reverse(
                        "site_admin:post_edit", kwargs={"slug": saved_post.slug}
                    )
                    return response
                refreshed_form = PostForm(instance=saved_post)
                return render(
                    request,
                    "site_admin/posts/_form_messages.html",
                    _build_post_form_context(
                        request=request,
                        form=refreshed_form,
                        post=saved_post,
                        saved=True,
                    ),
                )
            return redirect("site_admin:post_edit", slug=saved_post.slug)
        context = _build_post_form_context(
            request=request,
            form=form,
            post=post,
            saved=False,
            existing_meta=existing_meta,
            existing_remove_ids=existing_remove_ids,
            uploaded_meta=uploaded_meta,
        )
        template_name = (
            "site_admin/posts/_form_messages.html"
            if request.headers.get("HX-Request")
            else "site_admin/posts/edit.html"
        )
        return render(request, template_name, context)
    else:
        form = PostForm(instance=post)

    template_name = (
        "site_admin/posts/_form.html"
        if request.headers.get("HX-Request")
        else "site_admin/posts/edit.html"
    )
    return render(
        request,
        template_name,
        _build_post_form_context(request=request, form=form, post=post, saved=False),
    )


@require_POST
def post_delete(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    post = get_object_or_404(Post, slug=slug)
    post.deleted = True
    post.save(update_fields=["deleted"])
    return redirect("site_admin:post_list")


@require_POST
def post_permanent_delete(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    post = get_object_or_404(Post, slug=slug)
    post.delete()
    return redirect("site_admin:post_list")


@require_http_methods(["GET", "POST"])
def theme_settings(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    settings_obj = SiteConfiguration.get_solo()
    active_theme_slug = settings_obj.active_theme or ""
    active_theme = get_theme(active_theme_slug) if active_theme_slug else None
    theme_settings_schema = active_theme.settings_schema if active_theme else {}
    theme_settings_fields = (
        theme_settings_schema.get("fields")
        if isinstance(theme_settings_schema.get("fields"), dict)
        else {}
    )
    stored_theme_settings = (
        settings_obj.theme_settings if isinstance(settings_obj.theme_settings, dict) else {}
    )
    active_theme_settings = (
        stored_theme_settings.get(active_theme_slug, {}) if active_theme_slug else {}
    )
    resolved_theme_settings = resolve_theme_settings(theme_settings_schema, active_theme_settings)
    theme_settings_form = (
        ThemeSettingsForm(theme_settings_schema, initial=resolved_theme_settings)
        if theme_settings_fields
        else None
    )

    action = request.POST.get("action") if request.method == "POST" else ""
    install_source = request.POST.get("install_source") if request.method == "POST" else ""
    if action == "install_git":
        install_source = "git"
    if install_source not in {"upload", "git"}:
        install_source = "git"
    is_install_action = request.method == "POST" and action in {"install_theme", "install_git"}

    upload_form = ThemeUploadForm()
    git_form = ThemeGitInstallForm()
    if is_install_action and install_source == "upload":
        upload_form = ThemeUploadForm(request.POST or None, request.FILES or None)
    elif is_install_action and install_source == "git":
        git_form = ThemeGitInstallForm(request.POST or None)
    if request.method == "POST" and request.POST.get("action") == "save_theme_settings":
        if not (active_theme and theme_settings_fields):
            messages.error(request, "Active theme does not define any settings to edit.")
            return redirect("site_admin:theme_settings")
        theme_settings_form = ThemeSettingsForm(theme_settings_schema, request.POST)
        if theme_settings_form.is_valid():
            stored_theme_settings[active_theme_slug] = theme_settings_form.cleaned_data
            settings_obj.theme_settings = stored_theme_settings
            settings_obj.save(update_fields=["theme_settings"])
            clear_template_caches()
            messages.success(request, f"Saved settings for {active_theme.label}.")
            return redirect("site_admin:theme_settings")
    if request.method == "POST" and action == "theme_storage_healthcheck":
        write_test = request.POST.get("write_test") == "on"
        result = theme_storage_healthcheck(write_test=write_test)
        errors = result.get("errors") or []
        if result.get("ok"):
            if write_test:
                messages.success(request, "Theme storage healthcheck succeeded (read/write).")
            else:
                messages.success(request, "Theme storage healthcheck succeeded (read-only).")
        elif not errors:
            messages.error(request, "Theme storage healthcheck failed.")
        else:
            for error in errors:
                operation = error.get("operation") or "check"
                detail = error.get("message") or "Unknown error"
                hint = error.get("hint")
                hint_suffix = f" Hint: {hint}" if hint else ""
                messages.error(
                    request,
                    f"Theme storage healthcheck failed during {operation}: {detail}.{hint_suffix}",
                )
        return redirect("site_admin:theme_settings")

    if request.method == "POST" and action == "check_theme_storage":
        restored = []
        failures = []
        storage_synced = []

        try:
            results = reconcile_installed_themes()
            restored = [result.slug for result in results if result.restored]
            failures = [result.slug for result in results if result.status == ThemeInstall.STATUS_FAILED]
        except Exception as exc:  # pragma: no cover - defensive
            messages.error(request, f"Unable to check theme installs: {exc}")

        try:
            storage_synced = sync_themes_from_storage()
        except Exception as exc:  # pragma: no cover - defensive
            messages.warning(request, f"Unable to check theme storage: {exc}")

        if restored:
            restored_slugs = sorted(set(restored))
            restored_list = ", ".join(restored_slugs)
            messages.success(
                request, f"Restored {len(restored_slugs)} theme(s) from installs: {restored_list}."
            )
        if storage_synced:
            storage_slugs = sorted(set(storage_synced))
            storage_list = ", ".join(storage_slugs)
            messages.success(
                request, f"Synced {len(storage_slugs)} theme(s) from storage: {storage_list}."
            )
        if failures:
            messages.warning(
                request, f"Theme installs still failing for: {', '.join(sorted(set(failures)))}."
            )
        elif not any([restored, storage_synced]):
            messages.info(request, "No themes found in installs or storage to sync.")

        return redirect("site_admin:theme_settings")

    if is_install_action and install_source == "git":
        if git_form.is_valid():
            try:
                theme = install_theme_from_git(
                    git_form.cleaned_data["git_url"],
                    git_form.cleaned_data["slug"],
                    ref=git_form.cleaned_data.get("ref") or "",
                )
                messages.success(
                    request,
                    f"Theme '{theme.label}' ({theme.slug}) installed from git and synced to storage.",
                )
                return redirect("site_admin:theme_settings")
            except ThemeUploadError as exc:
                git_form.add_error(None, exc)
            except Exception as exc:  # pragma: no cover - defensive
                git_form.add_error(None, f"Unexpected error: {exc}")

    if is_install_action and install_source == "upload":
        if upload_form.is_valid():
            try:
                theme = ingest_theme_archive(upload_form.cleaned_data["archive"])
                messages.success(
                    request,
                    f"Theme '{theme.label}' ({theme.slug}) uploaded and synced to storage.",
                )
                return redirect("site_admin:theme_settings")
            except ThemeUploadError as exc:
                upload_form.add_error("archive", exc)
            except Exception as exc:  # pragma: no cover - defensive
                upload_form.add_error("archive", f"Unexpected error: {exc}")

    install_map = {install.slug: install for install in ThemeInstall.objects.all()}
    themes = discover_themes()
    theme_by_slug = {theme.slug: theme for theme in themes}
    file_counts = {
        theme.slug: len(list_theme_files(theme.slug, suffixes=ALLOWED_SUFFIXES))
        for theme in themes
    }
    all_slugs = sorted(set(theme_by_slug) | set(install_map))

    source_type = (request.GET.get("source_type") or "").strip()
    status = (request.GET.get("status") or "").strip()
    query = (request.GET.get("q") or "").strip()
    status_choices = list(ThemeInstall.STATUS_CHOICES) + [
        ("missing_local", "Missing locally"),
        ("untracked", "Untracked"),
    ]
    valid_statuses = {value for value, _ in status_choices}
    valid_sources = dict(ThemeInstall.SOURCE_CHOICES)

    inventory_rows = []
    for slug in all_slugs:
        theme = theme_by_slug.get(slug)
        install = install_map.get(slug)
        local_present = theme is not None
        if install and not local_present:
            effective_status = "missing_local"
            install_status_label = "Missing locally"
        elif install:
            effective_status = install.last_sync_status or ""
            install_status_label = (
                install.get_last_sync_status_display()
                if install.last_sync_status
                else "-"
            )
        else:
            effective_status = "untracked"
            install_status_label = "Untracked"

        row = {
            "slug": slug,
            "label": theme.label if theme else slug,
            "theme": theme,
            "install": install,
            "file_count": file_counts.get(slug, 0) if local_present else 0,
            "local_present": local_present,
            "effective_status": effective_status,
            "install_status_label": install_status_label,
            "source_label": install.get_source_type_display() if install else "Local",
            "source_ref": install.source_ref if install else "",
            "source_url": install.safe_source_url() if install else "",
            "version": (
                theme.version
                if theme and theme.version
                else (install.version if install and install.version else "-")
            ),
            "author": theme.author if theme and theme.author else "-",
            "last_synced_at": install.last_synced_at if install else None,
            "is_active": slug == active_theme_slug,
        }
        inventory_rows.append(row)

    filtered_rows = []
    for row in inventory_rows:
        if source_type in valid_sources:
            if not row["install"] or row["install"].source_type != source_type:
                continue
        if status in valid_statuses:
            if row["effective_status"] != status:
                continue
        if query:
            haystack = " ".join(
                filter(
                    None,
                    [
                        row["slug"],
                        row["label"],
                        row.get("source_ref") or "",
                        row.get("source_url") or "",
                    ],
                )
            ).lower()
            if query.lower() not in haystack:
                continue
        filtered_rows.append(row)
    theme_settings_groups = []
    theme_settings_ungrouped_fields = []
    if theme_settings_form:
        grouped_field_names = set()
        raw_groups = (
            theme_settings_schema.get("groups")
            if isinstance(theme_settings_schema.get("groups"), list)
            else []
        )
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            field_names = group.get("fields")
            if not isinstance(field_names, list):
                continue
            fields = []
            for field_name in field_names:
                if field_name in theme_settings_form.fields:
                    fields.append(theme_settings_form[field_name])
                    grouped_field_names.add(field_name)
            if fields:
                theme_settings_groups.append(
                    {
                        "label": group.get("label") or "Settings",
                        "fields": fields,
                    }
                )
        for field_name in theme_settings_form.fields:
            if field_name not in grouped_field_names:
                theme_settings_ungrouped_fields.append(theme_settings_form[field_name])

    return render(
        request,
            "site_admin/settings/themes/index.html",
        {
            "upload_form": upload_form,
            "git_form": git_form,
            "theme_settings_form": theme_settings_form,
            "theme_settings_schema": theme_settings_schema,
            "theme_settings_groups": theme_settings_groups,
            "theme_settings_ungrouped_fields": theme_settings_ungrouped_fields,
            "active_theme": active_theme,
            "themes": themes,
            "inventory_rows": filtered_rows,
            "active_theme_slug": active_theme_slug,
            "filters": {
                "source_type": source_type,
                "status": status,
                "q": query,
            },
            "source_choices": ThemeInstall.SOURCE_CHOICES,
            "status_choices": status_choices,
            "install_source": install_source,
        },
    )


@require_GET
def theme_git_refs(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    git_url = (request.GET.get("git_url") or "").strip()
    if not git_url:
        return JsonResponse({"refs": [], "default_ref": "", "error": "Missing git_url"}, status=400)

    default_ref = ""
    refs = []
    try:
        symref_result = subprocess.run(
            ["git", "ls-remote", "--symref", git_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        for line in symref_result.stdout.splitlines():
            if line.startswith("ref:"):
                ref_name = line.split("ref:", 1)[1].strip().split()[0]
                if ref_name.startswith("refs/heads/"):
                    default_ref = ref_name[len("refs/heads/") :]
                elif ref_name.startswith("refs/tags/"):
                    default_ref = ref_name[len("refs/tags/") :]
                else:
                    default_ref = ref_name
                break

        refs_result = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", git_url],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if refs_result.returncode != 0:
            raise RuntimeError(refs_result.stderr.strip() or "Unable to read git refs.")

        seen = set()
        for line in refs_result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            ref_name = parts[1]
            if ref_name.endswith("^{}"):
                continue
            if ref_name.startswith("refs/heads/"):
                ref_name = ref_name[len("refs/heads/") :]
            elif ref_name.startswith("refs/tags/"):
                ref_name = ref_name[len("refs/tags/") :]
            if ref_name and ref_name not in seen:
                seen.add(ref_name)
                refs.append(ref_name)
    except Exception as exc:
        return JsonResponse({"refs": [], "default_ref": "", "error": str(exc)}, status=400)

    if default_ref and default_ref not in refs:
        refs.insert(0, default_ref)

    return JsonResponse({"refs": refs, "default_ref": default_ref})


@require_http_methods(["GET", "POST"])
def theme_file_edit(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    themes = discover_themes()
    if not themes:
        messages.warning(request, "Upload a theme first to enable editing.")
        return redirect("site_admin:theme_settings")

    theme_choices = _theme_choices()
    selection = _build_theme_selection(request, slug)
    file_choices = (
        list_theme_files(selection.slug, suffixes=ALLOWED_SUFFIXES) if selection.slug else []
    )
    file_choices = [path for path in file_choices if not _is_git_path(path)]
    directory_choices = list_theme_directories(selection.slug) if selection.slug else []
    directory_choices = [path for path in directory_choices if not _is_git_path(path)]
    path_choices = sorted(set(file_choices + directory_choices))

    form_initial = {"theme": selection.slug, "path": selection.path, "content": selection.content}
    form = ThemeFileForm(theme_choices, path_choices, request.POST or None, initial=form_initial)

    if request.method == "POST" and form.is_valid():
        chosen_theme = form.cleaned_data["theme"]
        chosen_path = form.cleaned_data.get("path") or ""
        requested_name = (request.POST.get("new_entry_name") or "").strip().rstrip("/")

        if "load" in request.POST:
            return redirect(
                f"{reverse('site_admin:theme_file_edit', kwargs={'slug': chosen_theme})}?path={chosen_path}"
            )

        if "new_file" in request.POST:
            if not requested_name:
                messages.error(request, "Provide a name for the new file.")
                return redirect("site_admin:theme_file_edit", slug=chosen_theme)
            if requested_name.startswith("/") or "\\" in requested_name or ".." in requested_name:
                messages.error(
                    request, "Paths cannot start with '/' or contain backslashes or '..'."
                )
                return redirect("site_admin:theme_file_edit", slug=chosen_theme)

            target_dir = Path(chosen_path).parent if chosen_path else Path("")
            target_relative = (target_dir / requested_name).as_posix()

            try:
                if ALLOWED_SUFFIXES and Path(requested_name).suffix not in ALLOWED_SUFFIXES:
                    allowed = ", ".join(ALLOWED_SUFFIXES)
                    messages.error(
                        request, f"Files must use one of the allowed extensions: {allowed}"
                    )
                    return redirect("site_admin:theme_file_edit", slug=chosen_theme)
                create_theme_file(chosen_theme, target_relative)
                messages.success(request, f"Created file {target_relative} in {chosen_theme}.")
                next_path = target_relative
                return redirect(
                    f"{reverse('site_admin:theme_file_edit', kwargs={'slug': chosen_theme})}?path={next_path}"
                    if next_path
                    else reverse("site_admin:theme_file_edit", kwargs={"slug": chosen_theme})
                )
            except ThemeUploadError as exc:
                messages.error(request, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                messages.error(request, f"Unable to create entry: {exc}")
            return redirect("site_admin:theme_file_edit", slug=chosen_theme)

        if "delete" in request.POST:
            if not chosen_path:
                messages.error(request, "Select a file to delete.")
                return redirect("site_admin:theme_file_edit", slug=chosen_theme)
            try:
                delete_theme_path(chosen_theme, chosen_path)
                messages.success(request, f"Deleted {chosen_path} from {chosen_theme}.")
                next_path = next((p for p in file_choices if p != chosen_path), None)
                redirect_url = (
                    f"{reverse('site_admin:theme_file_edit', kwargs={'slug': chosen_theme})}?path={next_path}"
                    if next_path
                    else reverse("site_admin:theme_file_edit", kwargs={"slug": chosen_theme})
                )
                return redirect(redirect_url)
            except ThemeUploadError as exc:
                messages.error(request, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                messages.error(request, f"Unable to delete path: {exc}")
            return redirect("site_admin:theme_file_edit", slug=chosen_theme)

        if "save" not in request.POST:
            messages.error(request, "Use the Save file button to persist changes.")
            return redirect(
                f"{reverse('site_admin:theme_file_edit', kwargs={'slug': chosen_theme})}?path={chosen_path}"
            )

        if not chosen_path:
            messages.error(request, "Select a file to edit for this theme.")
            return redirect("site_admin:theme_file_edit", slug=chosen_theme)

        try:
            save_theme_file(chosen_theme, chosen_path, form.cleaned_data["content"])
            messages.success(request, f"Saved {chosen_path} in {chosen_theme}.")
            return redirect(
                f"{reverse('site_admin:theme_file_edit', kwargs={'slug': chosen_theme})}?path={chosen_path}"
            )
        except ThemeUploadError as exc:
            messages.error(request, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            messages.error(request, f"Unable to save file: {exc}")

    return render(
        request,
        "site_admin/settings/themes/edit.html",
        {
            "form": form,
            "themes": themes,
            "theme_choices": theme_choices,
            "file_choices": file_choices,
            "directory_choices": directory_choices,
            "selection": selection,
        },
    )


@require_http_methods(["GET", "POST"])
def theme_install_detail(request, slug):
    guard = _staff_guard(request)
    if guard:
        return guard

    install = get_object_or_404(ThemeInstall, slug=slug)
    if request.method == "POST":
        if request.POST.get("action") == "theme_storage_healthcheck":
            write_test = request.POST.get("write_test") == "on"
            result = theme_storage_healthcheck(write_test=write_test)
            errors = result.get("errors") or []
            if result.get("ok"):
                if write_test:
                    messages.success(request, "Theme storage healthcheck succeeded (read/write).")
                else:
                    messages.success(request, "Theme storage healthcheck succeeded (read-only).")
            elif not errors:
                messages.error(request, "Theme storage healthcheck failed.")
            else:
                for error in errors:
                    operation = error.get("operation") or "check"
                    detail = error.get("message") or "Unknown error"
                    hint = error.get("hint")
                    hint_suffix = f" Hint: {hint}" if hint else ""
                    messages.error(
                        request,
                        f"Theme storage healthcheck failed during {operation}: {detail}.{hint_suffix}",
                    )
            return redirect("site_admin:theme_install_detail", slug=slug)

        if install.source_type != ThemeInstall.SOURCE_GIT:
            messages.error(request, "Only git-installed themes can be updated.")
            return redirect("site_admin:theme_install_detail", slug=slug)

        ref_value = (request.POST.get("ref") or "").strip()
        ref = ref_value or None
        try:
            result = update_theme_from_git(install, ref=ref)
            if result.updated:
                messages.success(
                    request,
                    f"Updated {install.slug} to {result.commit or 'latest'} ({result.ref or 'default ref'}).",
                )
            else:
                messages.info(
                    request,
                    f"{install.slug} already up to date at {result.commit or 'latest'} ({result.ref or 'default ref'}).",
                )
        except ThemeUploadError as exc:
            messages.error(request, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            messages.error(request, f"Unable to update theme: {exc}")
        return redirect("site_admin:theme_install_detail", slug=slug)
    return render(
        request,
        "site_admin/settings/themes/install_detail.html",
        {
            "install": install,
            "source_url": install.safe_source_url(),
        },
    )


@require_http_methods(["GET", "POST"])
def site_settings(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    settings_obj = SiteConfiguration.get_solo()
    saved = False
    if request.method == "POST":
        form = SiteConfigurationForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            saved = True
    else:
        form = SiteConfigurationForm(instance=settings_obj)

    return render(
        request,
        "site_admin/settings/edit.html",
        {
            "form": form,
            "saved": saved,
        },
    )


@require_http_methods(["GET", "POST"])
def profile_edit(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    hcard = HCard.objects.filter(user=request.user).order_by("pk").first()
    parent_instance = hcard or HCard(user=request.user)
    saved = False
    existing_meta = None
    uploaded_meta = None
    existing_remove_ids = None

    if request.method == "POST":
        form = HCardForm(request.POST, instance=parent_instance)
        url_formset = HCardUrlFormSet(
            request.POST,
            instance=parent_instance,
            prefix="urls",
        )
        email_formset = HCardEmailFormSet(
            request.POST,
            instance=parent_instance,
            prefix="emails",
        )
        existing_meta = _parse_positioned_ids(
            request.POST.getlist("existing_ids"),
            request.POST.getlist("existing_positions"),
        )
        uploaded_meta = _parse_positioned_ids(
            request.POST.getlist("uploaded_ids"),
            request.POST.getlist("uploaded_positions"),
        )
        existing_remove_ids = set()
        for raw_id in request.POST.getlist("existing_remove_ids"):
            try:
                existing_remove_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        if form.is_valid() and url_formset.is_valid() and email_formset.is_valid():
            hcard = form.save(commit=False)
            if not hcard.user_id:
                hcard.user = request.user
            hcard.save()
            parent_instance = hcard
            url_formset.instance = hcard
            url_formset.save()
            email_formset.instance = hcard
            email_formset.save()
            _sync_profile_photos(
                request=request,
                hcard=hcard,
                existing_meta=existing_meta,
                existing_remove_ids=existing_remove_ids,
                uploaded_meta=uploaded_meta,
            )
            saved = True
            existing_meta = None
            uploaded_meta = None
            existing_remove_ids = set()
    else:
        initial = {}
        if not parent_instance.uid:
            initial["uid"] = request.build_absolute_uri("/")
        form = HCardForm(instance=parent_instance, initial=initial)
        url_formset = HCardUrlFormSet(instance=parent_instance, prefix="urls")
        email_formset = HCardEmailFormSet(instance=parent_instance, prefix="emails")

    return render(
        request,
        "site_admin/profile/edit.html",
        {
            "form": form,
            "url_formset": url_formset,
            "email_formset": email_formset,
            "existing_photos_json": json.dumps(
                _build_profile_photo_items(
                    request=request,
                    hcard=parent_instance,
                    existing_meta=existing_meta if request.method == "POST" else None,
                    existing_remove_ids=existing_remove_ids if request.method == "POST" else None,
                    uploaded_meta=uploaded_meta if request.method == "POST" else None,
                )
            ),
            "photo_upload_url": reverse("site_admin:profile_upload_photo"),
            "photo_delete_url": reverse("site_admin:profile_delete_photo"),
            "saved": saved,
        },
    )


@require_POST
def profile_url_delete(request, url_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    url = get_object_or_404(HCardUrl, pk=url_id, hcard__user=request.user)
    url.delete()
    return HttpResponse("")


@require_POST
def profile_email_delete(request, email_id):
    guard = _staff_guard(request)
    if guard:
        return guard

    email = get_object_or_404(HCardEmail, pk=email_id, hcard__user=request.user)
    email.delete()
    return HttpResponse("")


@require_POST
def profile_upload_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    upload = request.FILES.get("photo")
    if not upload:
        return JsonResponse({"error": "No file provided."}, status=400)

    asset = File.objects.create(kind=File.IMAGE, file=upload, owner=request.user)
    return JsonResponse({"id": asset.id, "url": asset.file.url})


@require_POST
def profile_delete_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    try:
        asset_id = int(request.POST.get("id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid id."}, status=400)

    try:
        asset = File.objects.get(id=asset_id, owner=request.user)
    except File.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)

    in_use_response = _file_in_use_response(asset)
    if in_use_response:
        return in_use_response

    asset.delete()
    return JsonResponse({"status": "deleted"})


def _build_post_form_context(
    *,
    request,
    form,
    post,
    saved,
    existing_meta=None,
    existing_remove_ids=None,
    uploaded_meta=None,
):
    existing_meta = existing_meta or {}
    existing_remove_ids = existing_remove_ids or set()
    uploaded_meta = uploaded_meta or {}
    gpx_defaults = _gpx_form_defaults(request)

    photo_items = []
    activity_gpx = None
    if post:
        for attachment in post.attachments.select_related("asset"):
            if attachment.role == "gpx":
                activity_gpx = {
                    "id": attachment.asset.id,
                    "url": attachment.asset.file.url,
                    "name": Path(attachment.asset.file.name).name,
                }
                continue
            if attachment.role != "photo":
                continue
            asset = attachment.asset
            if asset.id in existing_remove_ids:
                continue
            meta = existing_meta.get(asset.id, {})
            photo_items.append(
                {
                    "kind": "existing",
                    "id": asset.id,
                    "url": asset.file.url,
                    "alt": meta.get("alt", asset.alt_text),
                    "caption": meta.get("caption", asset.caption),
                    "order": meta.get("position", attachment.sort_order),
                }
            )

    if uploaded_meta:
        uploaded_assets = File.objects.filter(
            id__in=uploaded_meta.keys(), owner=request.user
        )
        for asset in uploaded_assets:
            meta = uploaded_meta.get(asset.id, {})
            photo_items.append(
                {
                    "kind": "uploaded",
                    "id": asset.id,
                    "url": asset.file.url,
                    "alt": meta.get("alt", ""),
                    "caption": meta.get("caption", ""),
                    "order": meta.get("position", 0),
                }
            )

    photo_items.sort(key=lambda item: item.get("order", 0))

    return {
        "form": form,
        "post": post,
        "saved": saved,
        "existing_photos_json": json.dumps(photo_items),
        "photo_upload_url": reverse("site_admin:post_upload_photo"),
        "photo_delete_url": reverse("site_admin:post_delete_photo"),
        "activity_gpx": activity_gpx,
        "gpx_trim_enabled": gpx_defaults["gpx_trim_enabled"],
        "gpx_trim_distance": gpx_defaults["gpx_trim_distance"],
        "gpx_blur_enabled": gpx_defaults["gpx_blur_enabled"],
        "gpx_remove_timestamps": gpx_defaults["gpx_remove_timestamps"],
    }


@require_POST
def upload_post_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    upload = request.FILES.get("photo")
    if not upload:
        return JsonResponse({"error": "No file provided."}, status=400)

    asset = File.objects.create(kind=File.IMAGE, file=upload, owner=request.user)
    return JsonResponse({"id": asset.id, "url": asset.file.url})


@require_POST
def delete_post_photo(request):
    guard = _staff_guard(request)
    if guard:
        return guard

    try:
        asset_id = int(request.POST.get("id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid id."}, status=400)

    try:
        asset = File.objects.get(id=asset_id, owner=request.user)
    except File.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)

    in_use_response = _file_in_use_response(asset)
    if in_use_response:
        return in_use_response

    asset.delete()
    return JsonResponse({"status": "deleted"})
