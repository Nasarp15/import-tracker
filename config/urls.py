from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("orders.urls")),
    # Uploaded photos are only shown to people who are logged in.
    re_path(
        r"^media/(?P<path>.*)$",
        login_required(serve),
        {"document_root": settings.MEDIA_ROOT},
    ),
]