from django.conf import settings
from django.urls import path, include
from django.conf.urls.static import static

urlpatterns = [
    path('', include('core.urls')),
    path('blog/', include('blog.urls')),
    path('admin/', include('site_admin.urls')),
    path('', include('micropub.urls')),
    path("analytics/", include("analytics.urls")),
]

handler500 = "core.views.server_error"

if settings.DEBUG:
    from debug_toolbar.toolbar import debug_toolbar_urls
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += debug_toolbar_urls()
