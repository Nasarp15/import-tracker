from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("orders/", views.order_list, name="order_list"),
    path("orders/new/", views.order_create, name="order_create"),
    path("orders/<int:pk>/edit/", views.order_edit, name="order_edit"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("stock/", views.stock_list, name="stock_list"),
    path("stock/new/", views.stock_create, name="stock_create"),
    path("stock/<int:pk>/", views.stock_edit, name="stock_edit"),
    path("shop/", views.shop, name="shop"),
    path("reports/", views.reports, name="reports"),
    path("reports/export.csv", views.reports_export_csv, name="reports_export_csv"),
    path("track/", views.track_home, name="track_home"),
    path("track/batch/<int:pk>/", views.track_batch, name="track_batch"),
    path("track/<str:source>/", views.track_batches, name="track_batches"),
    path("batches/", views.batch_list, name="batch_list"),
    path("batches/new/", views.batch_create, name="batch_create"),
    path("batches/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batches/<int:pk>/split/", views.batch_split, name="batch_split"),
    path(
        "batches/<int:pk>/customers/<int:customer_pk>/",
        views.batch_customer,
        name="batch_customer",
    ),
]