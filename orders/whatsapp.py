from decimal import Decimal
from urllib.parse import quote

# Put the business name here to sign off every message, e.g. "Ama's Imports".
# Leave it empty ("") to send messages without a sign-off.
BUSINESS_NAME = ""


def money(amount):
    return f"{Decimal(amount):,.2f}"


def _customer_first_name(customer):
    name = customer.name.strip()
    return name.split()[0] if name else "there"


def _first_name(order):
    return _customer_first_name(order.customer)


def _item_line(order):
    notes = " ".join(order.item_notes.split()) or "your item"
    return f"{notes} (qty {order.quantity})"


def _signoff():
    return f"\n{BUSINESS_NAME}" if BUSINESS_NAME else ""


def _link_to(phone, text):
    return f"https://wa.me/{phone.lstrip('+')}?text={quote(text, safe='')}"


def _link(order, text):
    return _link_to(order.customer.phone, text)


def confirmation_message(order, bal):
    cur = order.currency
    lines = [
        f"Hello {_first_name(order)},",
        "",
        "This is to confirm your order:",
        f"- Item: {_item_line(order)}",
        f"- Price: {cur} {money(order.quoted_price)}",
    ]
    if bal["item_paid"] > 0:
        lines.append(f"- Paid so far: {cur} {money(bal['item_paid'])}")
    if (bal["item_balance"] or 0) > 0:
        lines.append(f"- Balance to pay: {cur} {money(bal['item_balance'])}")
    if order.address:
        lines.append(f"- Delivery to: {order.address.details}")
    lines += [
        "",
        "The shipping fee is paid once your item arrives in Ghana.",
        "Please reply YES if everything is correct, or tell me what to change. Thank you!",
    ]
    return "\n".join(lines) + _signoff()


def arrival_message(order, bal):
    cur = order.currency
    owed = bal["shipping_balance"] or 0
    lines = [
        f"Hello {_first_name(order)},",
        "",
        "Good news, your order has arrived in Ghana!",
        f"- Item: {_item_line(order)}",
        f"- Shipping fee: {cur} {money(order.shipping_fee)}",
    ]
    if bal["shipping_paid"] > 0:
        lines.append(f"- Shipping paid: {cur} {money(bal['shipping_paid'])}")
    if owed > 0:
        lines.append(f"- Shipping balance: {cur} {money(owed)}")
    if order.address:
        lines.append(f"- Delivery to: {order.address.details}")
    lines.append("")
    if owed > 0:
        lines.append(
            "Please send the shipping fee and let me know when delivery suits you. Thank you!"
        )
    else:
        lines.append(
            "Your shipping is fully paid. Please let me know when delivery suits you. Thank you!"
        )
    return "\n".join(lines) + _signoff()


def confirmation_link(order, bal):
    return _link(order, confirmation_message(order, bal))


def arrival_link(order, bal):
    return _link(order, arrival_message(order, bal))


# ------------------------------------------------ one message per customer per batch


def _distinct_addresses(orders):
    return {o.address.details for o in orders if o.address}


def group_confirmation_message(batch, customer, orders):
    priced = [o for o in orders if o.quoted_price and o.stage != "delivered"]
    cur = priced[0].currency
    addresses = _distinct_addresses(priced)
    total = sum((o.quoted_price for o in priced), Decimal("0"))
    paid = sum((o.paid_total("item") for o in priced), Decimal("0"))
    balance = total - paid

    lines = [
        f"Hello {_customer_first_name(customer)},",
        "",
        f"This is to confirm your orders in {batch.label}:",
    ]
    for number, order in enumerate(priced, 1):
        line = f"{number}. {_item_line(order)} - {cur} {money(order.quoted_price)}"
        if len(addresses) > 1 and order.address:
            line += f" (deliver to: {order.address.details})"
        lines.append(line)
    lines += ["", f"Total: {cur} {money(total)}"]
    if paid > 0:
        lines.append(f"Paid so far: {cur} {money(paid)}")
    if balance > 0:
        lines.append(f"Balance to pay: {cur} {money(balance)}")
    if len(addresses) == 1:
        lines.append(f"Delivery to: {next(iter(addresses))}")
    lines += [
        "",
        "The shipping fee is paid once your items arrive in Ghana.",
        "Please reply YES if everything is correct, or tell me what to change. Thank you!",
    ]
    return "\n".join(lines) + _signoff()


def group_arrival_message(batch, customer, orders):
    ready = [
        o
        for o in orders
        if o.shipping_fee is not None and o.stage in ("arrived", "shipping_paid")
    ]
    cur = ready[0].currency
    addresses = _distinct_addresses(ready)
    total = sum((o.shipping_fee for o in ready), Decimal("0"))
    paid = sum((o.paid_total("shipping") for o in ready), Decimal("0"))
    owed = total - paid

    lines = [
        f"Hello {_customer_first_name(customer)},",
        "",
        "Good news, your items have arrived in Ghana!",
    ]
    for number, order in enumerate(ready, 1):
        line = f"{number}. {_item_line(order)} - shipping {cur} {money(order.shipping_fee)}"
        if len(addresses) > 1 and order.address:
            line += f" (deliver to: {order.address.details})"
        lines.append(line)
    lines += ["", f"Total shipping: {cur} {money(total)}"]
    if paid > 0:
        lines.append(f"Shipping paid: {cur} {money(paid)}")
    if owed > 0:
        lines.append(f"Shipping balance: {cur} {money(owed)}")
    if len(addresses) == 1:
        lines.append(f"Delivery to: {next(iter(addresses))}")
    lines.append("")
    if owed > 0:
        lines.append(
            "Please send the shipping fee and let me know when delivery suits you. Thank you!"
        )
    else:
        lines.append(
            "Your shipping is fully paid. Please let me know when delivery suits you. Thank you!"
        )
    return "\n".join(lines) + _signoff()


def group_confirmation_link(batch, customer, orders):
    return _link_to(customer.phone, group_confirmation_message(batch, customer, orders))


def group_arrival_link(batch, customer, orders):
    return _link_to(customer.phone, group_arrival_message(batch, customer, orders))


def stock_order_link(number, item):
    """WhatsApp link a customer taps to ask about a stock item. None if no number is set."""
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if not digits:
        return None
    text = (
        f"Hello, I would like to order: {item.name} (GHS {money(item.price)}). "
        "Is it still available?"
    )
    return f"https://wa.me/{digits}?text={quote(text, safe='')}"