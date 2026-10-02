from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render

from .forms import OrderForm
from .models import Order

STAGE_CHIPS = [
    ("", "All"),
    ("quoted", "Quoted"),
    ("in_transit", "In transit"),
    ("arrived", "Shipping due"),
    ("shipping_paid", "Ready"),
    ("delivered", "Delivered"),
]


@login_required
def order_list(request):
    orders = (
        Order.objects.active()
        .select_related("customer", "batch")
        .prefetch_related("photos")
    )

    stage = request.GET.get("stage", "")
    if stage:
        orders = orders.filter(stage=stage)

    q = request.GET.get("q", "").strip()
    if q:
        orders = orders.filter(
            Q(customer__name__icontains=q)
            | Q(customer__phone__icontains=q)
            | Q(item_notes__icontains=q)
        )

    context = {"orders": orders[:100], "chips": STAGE_CHIPS, "stage": stage, "q": q}
    return render(request, "orders/order_list.html", context)


@login_required
def order_create(request):
    if request.method == "POST":
        form = OrderForm(request.POST, request.FILES)
        if form.is_valid():
            form.save_order()
            return redirect("order_list")
    else:
        form = OrderForm()
    return render(request, "orders/order_form.html", {"form": form})