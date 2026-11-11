from decimal import Decimal

from django.db import transaction

from .models import Payment

ZERO = Decimal("0")


def _balance(order, kind):
    """How much is still owed on one order for item or shipping money."""
    if kind == Payment.Kind.ITEM:
        if not order.quoted_price:
            return ZERO
        owed = order.quoted_price - order.paid_total(kind)
    else:
        if order.shipping_fee is None:
            return ZERO
        owed = order.shipping_fee - order.paid_total(kind)
    return max(owed, ZERO)


def group_totals(orders):
    item_total = sum((o.quoted_price for o in orders if o.quoted_price), ZERO)
    item_paid = sum(
        (o.paid_total(Payment.Kind.ITEM) for o in orders if o.quoted_price), ZERO
    )
    shipping_total = sum(
        (o.shipping_fee for o in orders if o.shipping_fee is not None), ZERO
    )
    shipping_paid = sum(
        (o.paid_total(Payment.Kind.SHIPPING) for o in orders if o.shipping_fee is not None),
        ZERO,
    )
    return {
        "item_total": item_total,
        "item_paid": item_paid,
        "item_balance": max(item_total - item_paid, ZERO),
        "shipping_total": shipping_total,
        "shipping_paid": shipping_paid,
        "shipping_balance": max(shipping_total - shipping_paid, ZERO),
    }


def customer_groups(orders):
    """Group orders by customer (same phone number = same customer)."""
    grouped = {}
    for order in orders:
        entry = grouped.setdefault(
            order.customer_id, {"customer": order.customer, "orders": []}
        )
        entry["orders"].append(order)
    groups = []
    for entry in grouped.values():
        entry.update(group_totals(entry["orders"]))
        groups.append(entry)
    groups.sort(key=lambda g: (g["customer"].name or g["customer"].phone).lower())
    return groups


def allocate_payment(orders, kind, amount, method):
    """Spread one payment across a customer's orders, filling each balance in turn.

    Raises ValueError if the amount is more than the customer owes for `kind`.
    Returns the list of Payment rows created.
    """
    outstanding = [(o, _balance(o, kind)) for o in sorted(orders, key=lambda o: o.created_at)]
    outstanding = [(o, owed) for o, owed in outstanding if owed > 0]
    total_owed = sum((owed for _, owed in outstanding), ZERO)
    if total_owed == 0:
        raise ValueError("Nothing is owed for this payment type.")
    if amount > total_owed:
        raise ValueError(f"This is more than the amount owed ({total_owed:,.2f}).")

    created = []
    remaining = amount
    with transaction.atomic():
        for order, owed in outstanding:
            if remaining <= 0:
                break
            part = min(owed, remaining)
            payment = Payment(
                order=order,
                kind=kind,
                amount=part,
                currency=order.currency,
                method=method,
            )
            payment.save()
            created.append(payment)
            remaining -= part
    return created



def item_quantity_groups(orders):
    """
    Group orders by item name, calculate total quantities,
    and identify the customers who ordered each item.
    """
    grouped = {}

    for order in orders:
        stock_item = getattr(order, "stock_item", None)

        if stock_item:
            item_name = stock_item.name.strip()
        else:
            item_name = (order.item_notes or "").strip()

        if not item_name:
            item_name = "Unnamed item"

        key = " ".join(item_name.casefold().split())

        if key not in grouped:
            grouped[key] = {
                "name": item_name,
                "quantity": 0,
                "order_count": 0,
                "customers": {},
            }

        item = grouped[key]

        item["quantity"] += order.quantity
        item["order_count"] += 1

        customer_id = order.customer_id

        if customer_id not in item["customers"]:
            item["customers"][customer_id] = {
                "name": (
                    order.customer.name
                    or order.customer.phone
                ),
                "phone": order.customer.phone,
                "quantity": 0,
            }

        item["customers"][customer_id]["quantity"] += order.quantity

    results = []

    for item in grouped.values():
        item["customers"] = sorted(
            item["customers"].values(),
            key=lambda customer: customer["name"].casefold(),
        )
        results.append(item)

    return sorted(
        results,
        key=lambda item: item["name"].casefold(),
    )
