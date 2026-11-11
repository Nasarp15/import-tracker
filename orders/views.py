import csv
from datetime import timedelta
from decimal import Decimal


from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.cache import never_cache

from .forms import (
    BatchBillForm,
    BatchForm,
    BatchRateForm,
    OrderForm,
    PaymentForm,
    QuoteForm,
    ShippingForm,
    StockItemForm,
    TrackForm,
)
from .models import Batch, Customer, Order, Payment, StockItem
from .grouping import (
    allocate_payment,
    customer_groups,
    group_totals,
    item_quantity_groups,
)
from .profit import batch_profit
from .shipping import split_bill, target_total
from .tracking import (
    BATCH_STATUS,
    client_ip,
    customer_status,
    record_attempt,
    too_many_attempts,
)
from .whatsapp import (
    arrival_link,
    confirmation_link,
    group_arrival_link,
    group_confirmation_link,
    stock_order_link,
)

@login_required
def dashboard(request):
    """Staff overview with live summaries from the existing application data."""
    active_orders = Order.objects.active()
    open_batches = Batch.objects.exclude(status=Batch.Status.CLOSED)
    stock_items = StockItem.objects.all()

    context = {
        "active_orders_count": active_orders.count(),
        "in_transit_count": active_orders.filter(stage=Order.Stage.IN_TRANSIT).count(),
        "arrived_count": active_orders.filter(stage=Order.Stage.ARRIVED).count(),
        "open_batches_count": open_batches.count(),
        "stock_items_count": stock_items.count(),
        "low_stock_count": stock_items.filter(quantity__gt=0, quantity__lte=5).count(),
        "out_of_stock_count": stock_items.filter(quantity=0).count(),
        "recent_orders": active_orders.select_related("customer", "batch")[:6],
        "active_batches": open_batches.order_by("-id")[:5],
    }
    return render(request, "orders/dashboard.html", context)


STAGE_CHIPS = [
    ("", "All"),
    ("quoted", "Quoted"),
    ("in_transit", "In transit"),
    ("arrived", "Shipping due"),
    ("shipping_paid", "Ready"),
    ("delivered", "Delivered"),
]

NEXT_STAGE = {
    Order.Stage.PAID: (Order.Stage.PURCHASED, "Mark as purchased"),
    Order.Stage.PURCHASED: (Order.Stage.IN_TRANSIT, "Mark as in transit"),
    Order.Stage.IN_TRANSIT: (Order.Stage.ARRIVED, "Mark as arrived"),
    Order.Stage.SHIPPING_PAID: (Order.Stage.DELIVERED, "Mark as delivered"),
}


def next_step(order):
    """Return (stage, button label) for the next manual step, or None."""
    if (
        order.source_type == Order.SourceType.NIGERIA_STOCK
        and order.stage == Order.Stage.PAID
    ):
        return Order.Stage.DELIVERED, "Mark as delivered"
    return NEXT_STAGE.get(order.stage)


def balances(order):
    item_paid = order.paid_total(Payment.Kind.ITEM)
    shipping_paid = order.paid_total(Payment.Kind.SHIPPING)
    item_balance = order.quoted_price - item_paid if order.quoted_price else None
    shipping_balance = (
        order.shipping_fee - shipping_paid if order.shipping_fee is not None else None
    )
    return {
        "item_paid": item_paid,
        "shipping_paid": shipping_paid,
        "item_balance": item_balance,
        "shipping_balance": shipping_balance,
    }


def payment_initial(order, bal):
    if order.stage == Order.Stage.ARRIVED and (bal["shipping_balance"] or 0) > 0:
        return {"kind": Payment.Kind.SHIPPING, "amount": bal["shipping_balance"]}
    if (bal["item_balance"] or 0) > 0:
        return {"kind": Payment.Kind.ITEM, "amount": bal["item_balance"]}
    return {}


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
            order = form.save_order()
            return redirect("order_detail", pk=order.pk)
    else:
        form = OrderForm()
    return render(request, "orders/order_form.html", {"form": form})


@login_required
def order_edit(request, pk):
    order = get_object_or_404(Order.objects.active().select_related("customer", "address"), pk=pk)
    if request.method == "POST":
        form = OrderForm(request.POST, request.FILES, instance=order)
        if form.is_valid():
            try:
                updated_order = form.save_order()
            except forms.ValidationError as exc:
                # A stock quantity may have changed between loading and saving.
                form.add_error(None, exc)
            else:
                messages.success(request, "Order details updated successfully.")
                return redirect("order_detail", pk=updated_order.pk)
    else:
        form = OrderForm(instance=order)
    return render(
        request,
        "orders/order_edit.html",
        {"form": form, "order": order},
    )


@login_required
def order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.active()
        .select_related("customer", "address", "batch")
        .prefetch_related("photos", "payments"),
        pk=pk,
    )

    def fresh():
        return Order.objects.get(pk=order.pk)

    bal = balances(order)
    payment_form = PaymentForm(initial=payment_initial(order, bal))
    shipping_form = ShippingForm(instance=fresh())
    quote_form = QuoteForm(instance=fresh())

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_payment":
            payment_form = PaymentForm(request.POST)
            if payment_form.is_valid():
                kind = payment_form.cleaned_data["kind"]
                if kind == Payment.Kind.SHIPPING and order.shipping_fee is None:
                    payment_form.add_error("kind", "Set the shipping fee first.")
                else:
                    payment = payment_form.save(commit=False)
                    payment.order = order
                    payment.currency = order.currency
                    payment.save()
                    messages.success(request, "Payment recorded.")
                    return redirect("order_detail", pk=order.pk)

        elif action == "set_quote":
            if order.stage not in (Order.Stage.REQUESTED, Order.Stage.QUOTED):
                messages.error(request, "The quote can't be changed at this stage.")
                return redirect("order_detail", pk=order.pk)
            quote_form = QuoteForm(request.POST, instance=fresh())
            if quote_form.is_valid():
                updated = quote_form.save(commit=False)
                if updated.stage == Order.Stage.REQUESTED:
                    updated.stage = Order.Stage.QUOTED
                updated.save()
                updated.refresh_stage_from_payments()
                messages.success(request, "Quote saved.")
                return redirect("order_detail", pk=order.pk)

        elif action == "set_shipping":
            if order.stage != Order.Stage.ARRIVED:
                messages.error(request, "Shipping can only be set once the item has arrived.")
                return redirect("order_detail", pk=order.pk)
            shipping_form = ShippingForm(request.POST, instance=fresh())
            if shipping_form.is_valid():
                updated = shipping_form.save()
                updated.refresh_stage_from_payments()
                return redirect("order_detail", pk=order.pk)

        elif action == "set_stage":
            step = next_step(order)
            if step and request.POST.get("stage") == step[0]:
                fields = ["stage"]
                order.stage = step[0]
                if step[0] == Order.Stage.DELIVERED:
                    order.delivered_on = timezone.localdate()
                    fields.append("delivered_on")
                order.save(update_fields=fields)
            else:
                messages.error(request, "That step isn't available for this order.")
            return redirect("order_detail", pk=order.pk)

        elif action == "cancel":
            order.is_cancelled = True
            order.save(update_fields=["is_cancelled"])
            if order.stock_item_id:
                StockItem.objects.filter(pk=order.stock_item_id).update(
                    quantity=F("quantity") + order.quantity
                )
            return redirect("order_list")

        bal = balances(order)

    can_add_payment = (bal["item_balance"] or 0) > 0 or (bal["shipping_balance"] or 0) > 0
    context = {
        "order": order,
        "payments": order.payments.all(),
        "next_step": next_step(order),
        "can_add_payment": can_add_payment,
        "confirmation_link": (
            confirmation_link(order, bal)
            if order.quoted_price and order.stage != Order.Stage.DELIVERED
            else None
        ),
        "arrival_link": (
            arrival_link(order, bal)
            if order.shipping_fee is not None
            and order.stage in (Order.Stage.ARRIVED, Order.Stage.SHIPPING_PAID)
            else None
        ),
        "payment_form": payment_form,
        "shipping_form": shipping_form,
        "quote_form": quote_form,
        **bal,
    }
    return render(request, "orders/order_detail.html", context)


# ---------------------------------------------------------------- batches

BATCH_NEXT = {
    Batch.Status.ORDERING: ("ship", "Mark as shipped"),
    Batch.Status.SHIPPED: ("arrive", "Mark as arrived"),
    Batch.Status.ARRIVED: ("close", "Close batch"),
}

ASSIGNABLE_SOURCE = {
    Batch.Source.CHINA: Order.SourceType.CHINA_PREORDER,
    Batch.Source.NIGERIA: Order.SourceType.NIGERIA_PREORDER,
}


def batch_is_open(batch):
    return batch.status in (Batch.Status.ORDERING, Batch.Status.SHIPPED)


def assignable_orders(batch):
    """Paid or part-paid preorders from the same source that are not in any batch yet."""
    return (
        Order.objects.active()
        .filter(batch__isnull=True, source_type=ASSIGNABLE_SOURCE[batch.source])
        .filter(
            Q(stage__in=[Order.Stage.PAID, Order.Stage.PURCHASED])
            | Q(stage=Order.Stage.QUOTED, payments__kind=Payment.Kind.ITEM)
        )
        .distinct()
        .select_related("customer")
    )


@login_required
def batch_list(request):
    show_closed = request.GET.get("closed") == "1"
    batches = Batch.objects.annotate(
        order_count=Count("orders", filter=Q(orders__is_cancelled=False))
    )
    if show_closed:
        batches = batches.filter(status=Batch.Status.CLOSED)
    else:
        batches = batches.exclude(status=Batch.Status.CLOSED)
    return render(
        request,
        "orders/batch_list.html",
        {"batches": batches, "show_closed": show_closed},
    )


@login_required
def batch_create(request):
    if request.method == "POST":
        form = BatchForm(request.POST)
        if form.is_valid():
            batch = form.save()
            return redirect("batch_detail", pk=batch.pk)
    else:
        form = BatchForm()
    return render(request, "orders/batch_form.html", {"form": form})



@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(Batch, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "ship" and batch.status == Batch.Status.ORDERING:
            moved = batch.mark_shipped()
            messages.success(
                request,
                f"Batch shipped. {moved} orders moved to in transit."
            )

        elif action == "arrive" and batch.status in (
            Batch.Status.ORDERING,
            Batch.Status.SHIPPED,
        ):
            moved = batch.mark_arrived()
            messages.success(
                request,
                f"Batch arrived. {moved} orders moved to arrived."
            )

        elif action == "close" and batch.status == Batch.Status.ARRIVED:
            batch.status = Batch.Status.CLOSED
            batch.save(update_fields=["status"])
            return redirect("batch_list")

        elif action == "add_orders" and batch_is_open(batch):
            ids = list(
                assignable_orders(batch)
                .filter(pk__in=request.POST.getlist("order_ids"))
                .values_list("pk", flat=True)
            )

            Order.objects.filter(pk__in=ids).update(batch=batch)

            if batch.status == Batch.Status.SHIPPED:
                Order.objects.filter(
                    pk__in=ids,
                    stage__in=[
                        Order.Stage.QUOTED,
                        Order.Stage.PAID,
                        Order.Stage.PURCHASED,
                    ],
                ).update(stage=Order.Stage.IN_TRANSIT)

            messages.success(request, f"{len(ids)} orders added.")

        elif action == "set_rate" and batch.status != Batch.Status.CLOSED:
            rate_form = BatchRateForm(request.POST, instance=batch)

            if rate_form.is_valid():
                rate_form.save()
                messages.success(request, "Exchange rate saved.")
            else:
                messages.error(
                    request,
                    "Enter an exchange rate greater than zero."
                )

        elif action == "remove_order" and batch_is_open(batch):
            Order.objects.filter(
                pk=request.POST.get("order_id"),
                batch=batch,
            ).update(batch=None)

            messages.success(request, "Order removed from the batch.")

        else:
            messages.error(
                request,
                "That action isn't available for this batch."
            )

        return redirect("batch_detail", pk=batch.pk)

    orders = (
        batch.orders.active()
        .select_related("customer", "batch", "stock_item")
        .prefetch_related("payments")
    )

    order_list = list(orders)

    # Existing customer grouping.
    groups = customer_groups(order_list)

    # New item grouping and total quantity calculation.
    item_groups = item_quantity_groups(order_list)

    to_collect = sum(
        (
            group["item_balance"] + group["shipping_balance"]
            for group in groups
        ),
        Decimal("0"),
    )

    context = {
        "batch": batch,
        "orders": orders,
        "groups": groups,
        "item_groups": item_groups,
        "to_collect": to_collect,
        "profit": (
            batch_profit(batch, order_list)
            if batch.status != Batch.Status.ORDERING
            else None
        ),
        "total": orders.count(),
        "shipping_due": orders.filter(
            stage=Order.Stage.ARRIVED
        ).count(),
        "delivered": orders.filter(
            stage=Order.Stage.DELIVERED
        ).count(),
        "next_action": BATCH_NEXT.get(batch.status),
        "is_open": batch_is_open(batch),
        "rate_form": BatchRateForm(instance=batch),
        "assignable": (
            assignable_orders(batch)
            if batch_is_open(batch)
            else []
        ),
    }

    return render(
        request,
        "orders/batch_detail.html",
        context,
    )


@login_required
def batch_split(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    orders = list(
        batch.orders.active().select_related("customer", "batch").prefetch_related("payments")
    )
    arrived = [o for o in orders if o.stage == Order.Stage.ARRIVED]

    if request.method == "POST":
        action = request.POST.get("action")

        if batch.status != Batch.Status.ARRIVED:
            messages.error(request, "The bill can only be split once the batch has arrived.")

        elif action == "set_bill":
            form = BatchBillForm(request.POST, instance=batch)
            if form.is_valid():
                form.save()
                messages.success(request, "Bill saved.")
            else:
                messages.error(request, "Enter the agent's bill as an amount greater than zero.")

        elif action == "apply":
            target = target_total(batch)
            if target is None:
                messages.error(request, "Enter the agent's bill first.")
            elif any(o.paid_total(Payment.Kind.SHIPPING) > 0 for o in arrived):
                messages.error(
                    request,
                    "Some customers have already paid shipping. Change individual fees on their order pages instead.",
                )
            else:
                shares, missing = split_bill(arrived, target)
                if not shares:
                    messages.error(
                        request,
                        "No order has a cost in cedis yet. Enter each order's cost, and the batch exchange rate for costs in yuan or naira.",
                    )
                else:
                    for order in arrived:
                        if order.pk in shares:
                            order.shipping_fee = shares[order.pk]
                            order.save(update_fields=["shipping_fee"])
                    text = f"Fees applied to {len(shares)} orders."
                    if missing:
                        text += f" {len(missing)} orders have no cost in cedis and were left out."
                    messages.success(request, text)

        else:
            messages.error(request, "That action isn't available.")
        return redirect("batch_split", pk=batch.pk)

    target = target_total(batch)
    shares, missing = split_bill(arrived, target) if target else ({}, [])
    rows = [
        {"order": o, "cost": o.cost_in_grid_currency, "share": shares.get(o.pk)}
        for o in arrived
    ]
    fees_set = sum((o.shipping_fee for o in orders if o.shipping_fee is not None), Decimal("0"))
    context = {
        "batch": batch,
        "rows": rows,
        "bill_form": BatchBillForm(instance=batch),
        "target": target,
        "fees_set": fees_set,
        "difference": (target - fees_set) if target else None,
        "needs_cost": len(missing),
        "can_edit": batch.status == Batch.Status.ARRIVED,
    }
    return render(request, "orders/batch_split.html", context)


@login_required
def batch_customer(request, pk, customer_pk):
    """All of one customer's items in one batch, with combined messages and payments."""
    batch = get_object_or_404(Batch, pk=pk)
    orders = list(
        batch.orders.active()
        .filter(customer_id=customer_pk)
        .select_related("customer", "address", "batch")
        .prefetch_related("payments")
        .order_by("created_at")
    )
    if not orders:
        raise Http404("No orders for this customer in this batch.")
    customer = orders[0].customer
    totals = group_totals(orders)

    initial = {}
    if totals["shipping_balance"] > 0:
        initial = {"kind": Payment.Kind.SHIPPING, "amount": totals["shipping_balance"]}
    elif totals["item_balance"] > 0:
        initial = {"kind": Payment.Kind.ITEM, "amount": totals["item_balance"]}
    form = PaymentForm(initial=initial)

    if request.method == "POST" and request.POST.get("action") == "record_payment":
        form = PaymentForm(request.POST)
        if form.is_valid():
            try:
                created = allocate_payment(
                    orders,
                    form.cleaned_data["kind"],
                    form.cleaned_data["amount"],
                    form.cleaned_data["method"],
                )
            except ValueError as error:
                form.add_error("amount", str(error))
            else:
                messages.success(
                    request, f"Payment recorded across {len(created)} item(s)."
                )
                return redirect("batch_customer", pk=batch.pk, customer_pk=customer.pk)

    priced = [
        o for o in orders if o.quoted_price and o.stage != Order.Stage.DELIVERED
    ]
    arrived = [
        o
        for o in orders
        if o.shipping_fee is not None
        and o.stage in (Order.Stage.ARRIVED, Order.Stage.SHIPPING_PAID)
    ]
    context = {
        "batch": batch,
        "customer": customer,
        "orders": orders,
        "totals": totals,
        "form": form,
        "can_pay": totals["item_balance"] > 0 or totals["shipping_balance"] > 0,
        "confirmation_link": (
            group_confirmation_link(batch, customer, orders) if priced else None
        ),
        "arrival_link": group_arrival_link(batch, customer, orders) if arrived else None,
    }
    return render(request, "orders/batch_customer.html", context)


# ------------------------------------------------------------------ stock


@login_required
def stock_list(request):
    return render(request, "orders/stock_list.html", {"items": StockItem.objects.all()})


@login_required
def stock_create(request):
    if request.method == "POST":
        form = StockItemForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect("stock_list")
    else:
        form = StockItemForm()
    return render(request, "orders/stock_form.html", {"form": form, "item": None})


@login_required
def stock_edit(request, pk):
    item = get_object_or_404(StockItem, pk=pk)
    if request.method == "POST" and request.POST.get("action") == "sold_out":
        item.quantity = 0
        item.save(update_fields=["quantity"])
        return redirect("stock_list")
    if request.method == "POST":
        form = StockItemForm(request.POST, request.FILES, instance=item)
        if form.is_valid():
            form.save()
            return redirect("stock_list")
    else:
        form = StockItemForm(instance=item)
    return render(request, "orders/stock_form.html", {"form": form, "item": item})


def shop(request):
    """Public page: show only visible items that have stock available."""
    number = getattr(settings, "BUSINESS_WHATSAPP", "")
    items = StockItem.objects.filter(is_visible=True, quantity__gt=0).order_by("name")
    rows = []
    for item in items:
        in_stock = item.quantity > 0
        rows.append(
            {
                "item": item,
                "sold_out": False,
                "link": stock_order_link(number, item) if in_stock else None,
                "size_link": (
                    stock_order_link(number, item, with_size=True)
                    if in_stock and item.size_list
                    else None
                ),
            }
        )
    return render(request, "orders/shop.html", {"rows": rows, **_public_context()})


# ------------------------------------------------- public order tracking (no login)


def _public_context(**extra):
    return {"business_name": getattr(settings, "BUSINESS_NAME", "Our shop"), **extra}


@never_cache
def track_home(request):
    """Let a customer find all active orders using the phone number they ordered with."""
    form = TrackForm(request.POST or None)
    orders = None
    customer = None
    blocked = False
    totals = None

    if request.method == "POST" and form.is_valid():
        phone = form.cleaned_data["phone"]
        ip = client_ip(request)
        if too_many_attempts(ip, phone):
            blocked = True
        else:
            record_attempt(ip, phone)
            customer = Customer.objects.filter(phone=phone).first()
            if customer:
                orders = list(
                    customer.orders.active()
                    .select_related("batch", "stock_item")
                    .prefetch_related("payments")
                    .order_by("-created_at")[:100]
                )
                # Calculate customer-visible balances from the already loaded payments.
                item_total = item_paid = shipping_total = shipping_paid = Decimal("0")
                for order in orders:
                    paid_items = sum(
                        (p.amount for p in order.payments.all() if p.kind == Payment.Kind.ITEM),
                        Decimal("0"),
                    )
                    paid_shipping = sum(
                        (p.amount for p in order.payments.all() if p.kind == Payment.Kind.SHIPPING),
                        Decimal("0"),
                    )
                    if order.quoted_price is not None:
                        item_total += order.quoted_price
                        item_paid += paid_items
                    if order.shipping_fee is not None:
                        shipping_total += order.shipping_fee
                        shipping_paid += paid_shipping
                    order.customer_status_label = customer_status(order)
                    timeline = [
                        (Order.Stage.REQUESTED, "Order received"),
                        (Order.Stage.QUOTED, "Quote prepared"),
                        (Order.Stage.PAID, "Payment received"),
                        (Order.Stage.PURCHASED, "Item purchased"),
                        (Order.Stage.IN_TRANSIT, "On its way to Ghana"),
                        (Order.Stage.ARRIVED, "Arrived in Ghana"),
                        (Order.Stage.SHIPPING_PAID, "Ready for delivery"),
                        (Order.Stage.DELIVERED, "Delivered"),
                    ]
                    current_index = next(
                        (i for i, (stage, _label) in enumerate(timeline) if stage == order.stage),
                        0,
                    )
                    order.progress_steps = [
                        {"label": label, "complete": i <= current_index, "current": i == current_index}
                        for i, (_stage, label) in enumerate(timeline)
                    ]
                    order.item_paid_display = paid_items
                    order.item_balance_display = max(
                        (order.quoted_price or Decimal("0")) - paid_items, Decimal("0")
                    )
                    order.shipping_paid_display = paid_shipping
                    order.shipping_balance_display = (
                        max(order.shipping_fee - paid_shipping, Decimal("0"))
                        if order.shipping_fee is not None else None
                    )
                totals = {
                    "item_total": item_total,
                    "item_paid": item_paid,
                    "item_balance": max(item_total - item_paid, Decimal("0")),
                    "shipping_total": shipping_total,
                    "shipping_paid": shipping_paid,
                    "shipping_balance": max(shipping_total - shipping_paid, Decimal("0")),
                }
            else:
                orders = []

    context = _public_context(
        form=form,
        orders=orders,
        customer=customer,
        totals=totals,
        blocked=blocked,
    )
    response = render(request, "orders/track_home.html", context)
    if blocked:
        response.status_code = 429
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@never_cache
def track_batches(request, source):
    if source not in (Batch.Source.CHINA, Batch.Source.NIGERIA):
        raise Http404
    batches = (
        Batch.objects.filter(source=source)
        .exclude(status=Batch.Status.CLOSED)
        .order_by("-id")[:12]
    )
    rows = [{"batch": b, "status": BATCH_STATUS[b.status]} for b in batches]
    context = _public_context(rows=rows, source=Batch.Source(source).label)
    return render(request, "orders/track_batches.html", context)


@never_cache
def track_batch(request, pk):
    batch = get_object_or_404(Batch.objects.exclude(status=Batch.Status.CLOSED), pk=pk)
    form = TrackForm(request.POST or None)
    items = None
    blocked = False
    first_name = ""

    if request.method == "POST" and form.is_valid():
        phone = form.cleaned_data["phone"]
        ip = client_ip(request)
        if too_many_attempts(ip, phone):
            blocked = True
        else:
            record_attempt(ip, phone)
            customer = Customer.objects.filter(phone=phone).first()
            orders = (
                list(batch.orders.active().filter(customer=customer).order_by("created_at"))
                if customer
                else []
            )
            items = [
                {
                    "notes": " ".join(o.item_notes.split()) or "Your item",
                    "quantity": o.quantity,
                    "status": customer_status(o),
                }
                for o in orders
            ]
            if customer and customer.name:
                first_name = customer.name.split()[0]

    context = _public_context(
        batch=batch,
        batch_status=BATCH_STATUS[batch.status],
        form=form,
        items=items,
        blocked=blocked,
        first_name=first_name,
    )
    response = render(request, "orders/track_batch.html", context)
    if blocked:
        response.status_code = 429
    return response

# --------------------------------------------------------------- reports

def _report_period(request):
    today = timezone.localdate()
    default_start = today - timedelta(days=29)
    start = parse_date(request.GET.get("start", "")) or default_start
    end = parse_date(request.GET.get("end", "")) or today
    if start > end:
        start, end = end, start
    return start, end


def _build_report_data(start, end):
    orders = list(
        Order.objects.active()
        .filter(created_at__date__range=(start, end))
        .select_related("customer", "batch", "stock_item")
        .prefetch_related("payments")
        .order_by("-created_at")
    )
    sales = sum((o.quoted_price or Decimal("0") for o in orders), Decimal("0"))
    item_outstanding = Decimal("0")
    shipping_outstanding = Decimal("0")
    for order in orders:
        item_paid = sum(
            (p.amount for p in order.payments.all() if p.kind == Payment.Kind.ITEM),
            Decimal("0"),
        )
        shipping_paid = sum(
            (p.amount for p in order.payments.all() if p.kind == Payment.Kind.SHIPPING),
            Decimal("0"),
        )
        if order.quoted_price is not None:
            item_outstanding += max(order.quoted_price - item_paid, Decimal("0"))
        if order.shipping_fee is not None:
            shipping_outstanding += max(order.shipping_fee - shipping_paid, Decimal("0"))

    payments_received = Payment.objects.filter(
        order__is_cancelled=False, paid_on__range=(start, end), currency="GHS"
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    product_rows = item_quantity_groups(orders)[:10]
    batch_ids = {o.batch_id for o in orders if o.batch_id}
    batch_rows = []
    for batch in Batch.objects.filter(pk__in=batch_ids).order_by("-id"):
        batch_orders = list(
            batch.orders.active()
            .select_related("customer", "batch")
            .prefetch_related("payments")
        )
        batch_rows.append({"batch": batch, "profit": batch_profit(batch, batch_orders)})

    return {
        "orders": orders,
        "order_count": len(orders),
        "sales": sales,
        "payments_received": payments_received,
        "item_outstanding": item_outstanding,
        "shipping_outstanding": shipping_outstanding,
        "total_outstanding": item_outstanding + shipping_outstanding,
        "products": product_rows,
        "batch_rows": batch_rows,
    }


@login_required
def reports(request):
    start, end = _report_period(request)
    data = _build_report_data(start, end)
    return render(
        request,
        "orders/reports.html",
        {"start": start, "end": end, **data},
    )


@login_required
def reports_export_csv(request):
    start, end = _report_period(request)
    data = _build_report_data(start, end)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="import-tracker-report-{start}-{end}.csv"'
    )
    response.write("\ufeff")  # Excel-friendly UTF-8 BOM.
    writer = csv.writer(response)
    writer.writerow(["Import Tracker report", f"{start} to {end}"])
    writer.writerow([])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Orders", data["order_count"]])
    writer.writerow(["Quoted item sales", data["sales"]])
    writer.writerow(["Payments received during period", data["payments_received"]])
    writer.writerow(["Outstanding item payments", data["item_outstanding"]])
    writer.writerow(["Outstanding shipping payments", data["shipping_outstanding"]])
    writer.writerow(["Total outstanding", data["total_outstanding"]])
    writer.writerow([])
    writer.writerow(["Best-selling products", "Quantity", "Orders"])
    for product in data["products"]:
        writer.writerow([product["name"], product["quantity"], product["order_count"]])
    writer.writerow([])
    writer.writerow(["Batch", "Sales (GHS)", "Cost (GHS)", "Item profit (GHS)", "Shipping result (GHS)", "Estimated total (GHS)"])
    for row in data["batch_rows"]:
        profit = row["profit"]
        writer.writerow([
            row["batch"].label, profit["sales"], profit["cost"],
            profit["item_profit"], profit["shipping_result"], profit["total"],
        ])
    return response
