from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import BatchForm, OrderForm, PaymentForm, QuoteForm, ShippingForm
from .models import Batch, Order, Payment
from .whatsapp import arrival_link, confirmation_link

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
        .select_related("customer", "address", "batch", "size_band")
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
            old_fee = order.shipping_fee
            shipping_form = ShippingForm(request.POST, instance=fresh())
            if shipping_form.is_valid():
                updated = shipping_form.save(commit=False)
                if updated.shipping_fee is None:
                    updated.shipping_fee = updated.suggested_shipping_fee() or old_fee
                    if updated.shipping_fee is None:
                        messages.warning(
                            request,
                            "No grid rule matches this size and cost. Enter the fee yourself.",
                        )
                updated.save()
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
        "suggested_fee": order.suggested_shipping_fee() if order.size_band else None,
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

        elif action == "remove_order" and batch_is_open(batch):
            Order.objects.filter(pk=request.POST.get("order_id"), batch=batch).update(
                batch=None
            )
            messages.success(request, "Order removed from the batch.")

        else:
            messages.error(request, "That action isn't available for this batch.")
        return redirect("batch_detail", pk=batch.pk)

    orders = batch.orders.active().select_related("customer")
    context = {
        "batch": batch,
        "orders": orders,
        "total": orders.count(),
        "shipping_due": orders.filter(stage=Order.Stage.ARRIVED).count(),
        "delivered": orders.filter(stage=Order.Stage.DELIVERED).count(),
        "next_action": BATCH_NEXT.get(batch.status),
        "is_open": batch_is_open(batch),
        "assignable": assignable_orders(batch) if batch_is_open(batch) else [],
    }
    return render(request, "orders/batch_detail.html", context)