from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from django.conf.urls.static import static

urlpatterns = [
    path('', include('core.urls')),
    path('blog/', include('blog.urls')),
    path('', include('micropub.urls')),
    path('admin/', admin.site.urls),
    path('mdeditor/', include('mdeditor.urls')),
]

if settings.DEBUG:
    from debug_toolbar.toolbar import debug_toolbar_urls
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += debug_toolbar_urls()
