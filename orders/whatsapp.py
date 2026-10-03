from decimal import Decimal
from urllib.parse import quote

# Put the business name here to sign off every message, e.g. "Ama's Imports".
# Leave it empty ("") to send messages without a sign-off.
BUSINESS_NAME = ""


def money(amount):
    return f"{Decimal(amount):,.2f}"


def _first_name(order):
    name = order.customer.name.strip()
    return name.split()[0] if name else "there"


def _item_line(order):
    notes = " ".join(order.item_notes.split()) or "your item"
    return f"{notes} (qty {order.quantity})"


def _signoff():
    return f"\n{BUSINESS_NAME}" if BUSINESS_NAME else ""


def _link(order, text):
    digits = order.customer.phone.lstrip("+")
    return f"https://wa.me/{digits}?text={quote(text, safe='')}"


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