from django.contrib import admin
from django.db.models import Count, Q
from .models import Visit


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ("path", "session_key", "user", "response_status_code", "started_at")
    date_hierarchy = "started_at"
    search_fields = ("path", "session_key", "user__username", "ip_address")
    list_filter = ("response_status_code", "country", "region")

    # Use a custom template instead of the default change_list
    change_list_template = "admin/visit_dashboard.html"

    def changelist_view(self, request, extra_context=None):
        qs = self.get_queryset(request)

        # Basic stats
        stats = qs.aggregate(
            total_page_views=Count("id"),
            unique_sessions=Count("session_key", distinct=True),
            unique_users=Count("user", distinct=True),
            unique_ips=Count("ip_address", distinct=True),
        )

        # Visitors by country
        visitors_by_country = (
            qs.values("country")
            .exclude(country="")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        # Any error responses (4xx / 5xx)
        error_visits = (
            qs.filter(response_status_code__gte=400)
            .select_related("user")
            .order_by("-started_at")[:10]
        )

        extra_context = extra_context or {}
        extra_context.update(
            stats=stats,
            visitors_by_country=visitors_by_country,
            error_visits=error_visits,
        )
        return super().changelist_view(request, extra_context=extra_context)
