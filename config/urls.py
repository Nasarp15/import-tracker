from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("orders.urls")),
    # Shop photos are public, so customers can see them without logging in.
    re_path(
        r"^media/stock/(?P<path>.*)$",
        serve,
        {"document_root": settings.MEDIA_ROOT / "stock"},
    ),
    # Everything else uploaded (customers' screenshots) is for logged-in staff only.
    re_path(
        r"^media/(?P<path>.*)$",
        login_required(serve),
        {"document_root": settings.MEDIA_ROOT},
    ),
]