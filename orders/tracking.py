from datetime import timedelta

from django.utils import timezone

from .models import Batch, LookupAttempt, Order

# How many lookups are allowed before the page asks people to wait.
IP_LIMIT, IP_WINDOW = 30, timedelta(minutes=10)
PHONE_LIMIT, PHONE_WINDOW = 5, timedelta(hours=1)

# What customers see instead of the internal stage names.
CUSTOMER_STATUS = {
    Order.Stage.REQUESTED: "Order received",
    Order.Stage.QUOTED: "Waiting for payment",
    Order.Stage.PAID: "Payment received",
    Order.Stage.PURCHASED: "Item purchased",
    Order.Stage.IN_TRANSIT: "On its way to Ghana",
    Order.Stage.ARRIVED: "Arrived in Ghana",
    Order.Stage.SHIPPING_PAID: "Ready for delivery",
    Order.Stage.DELIVERED: "Delivered",
}

BATCH_STATUS = {
    Batch.Status.ORDERING: "Collecting orders",
    Batch.Status.SHIPPED: "Shipped",
    Batch.Status.ARRIVED: "Arrived in Ghana",
    Batch.Status.CLOSED: "Completed",
}


def client_ip(request):
    """The visitor's address. Behind a proxy, the last entry is the one the proxy added."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:45]
    return request.META.get("REMOTE_ADDR", "")[:45]


def too_many_attempts(ip, phone):
    now = timezone.now()
    LookupAttempt.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
    by_ip = LookupAttempt.objects.filter(ip=ip, created_at__gte=now - IP_WINDOW).count()
    by_phone = LookupAttempt.objects.filter(
        phone=phone, created_at__gte=now - PHONE_WINDOW
    ).count()
    return by_ip >= IP_LIMIT or by_phone >= PHONE_LIMIT


def record_attempt(ip, phone):
    LookupAttempt.objects.create(ip=ip, phone=phone[:20])