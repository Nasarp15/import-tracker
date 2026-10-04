from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    BatchBillForm,
    BatchForm,
    BatchRateForm,
    OrderForm,
    PaymentForm,
    QuoteForm,
    ShippingForm,
)
from .models import Batch, Order, Payment
from .grouping import allocate_payment, customer_groups, group_totals
from .profit import batch_profit
from .shipping import split_bill, target_total
from .whatsapp import (
    arrival_link,
    confirmation_link,
    group_arrival_link,
    group_confirmation_link,
)

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
    """Paid preorders from the same source that are not in any batch yet."""
    return (
        Order.objects.active()
        .filter(
            batch__isnull=True,
            source_type=ASSIGNABLE_SOURCE[batch.source],
            stage__in=[Order.Stage.PAID, Order.Stage.PURCHASED],
        )
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
            messages.success(request, f"Batch shipped. {moved} orders moved to in transit.")

        elif action == "arrive" and batch.status in (
            Batch.Status.ORDERING,
            Batch.Status.SHIPPED,
        ):
            moved = batch.mark_arrived()
            messages.success(request, f"Batch arrived. {moved} orders moved to arrived.")

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
                    stage__in=[Order.Stage.PAID, Order.Stage.PURCHASED],
                ).update(stage=Order.Stage.IN_TRANSIT)
            messages.success(request, f"{len(ids)} orders added.")

        elif action == "set_rate" and batch.status != Batch.Status.CLOSED:
            rate_form = BatchRateForm(request.POST, instance=batch)
            if rate_form.is_valid():
                rate_form.save()
                messages.success(request, "Exchange rate saved.")
            else:
                messages.error(request, "Enter an exchange rate greater than zero.")

        elif action == "remove_order" and batch_is_open(batch):
            Order.objects.filter(pk=request.POST.get("order_id"), batch=batch).update(
                batch=None
            )
            messages.success(request, "Order removed from the batch.")

        else:
            messages.error(request, "That action isn't available for this batch.")
        return redirect("batch_detail", pk=batch.pk)

    orders = batch.orders.active().select_related("customer", "batch").prefetch_related("payments")
    order_list = list(orders)
    context = {
        "batch": batch,
        "orders": orders,
        "groups": customer_groups(order_list),
        "profit": (
            batch_profit(batch, order_list)
            if batch.status != Batch.Status.ORDERING
            else None
        ),
        "total": orders.count(),
        "shipping_due": orders.filter(stage=Order.Stage.ARRIVED).count(),
        "delivered": orders.filter(stage=Order.Stage.DELIVERED).count(),
        "next_action": BATCH_NEXT.get(batch.status),
        "is_open": batch_is_open(batch),
        "rate_form": BatchRateForm(instance=batch),
        "assignable": assignable_orders(batch) if batch_is_open(batch) else [],
    }
    return render(request, "orders/batch_detail.html", context)


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