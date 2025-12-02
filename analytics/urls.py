from django.urls import path
from .views import beacon_leave

urlpatterns = [
    path("leave/", beacon_leave, name="analytics-leave"),
]
