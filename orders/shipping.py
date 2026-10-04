from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def order_weight(order):
    """An order's share of the bill is proportional to its cost in cedis."""
    cost = order.cost_in_grid_currency
    return cost if cost and cost > 0 else None


def target_total(batch):
    """The agent's bill plus the optional extra percentage, or None if no bill yet."""
    if not batch.freight_total:
        return None
    margin = batch.freight_margin_percent or Decimal("0")
    factor = 1 + margin / Decimal("100")
    return (batch.freight_total * factor).quantize(CENT, rounding=ROUND_HALF_UP)


def split_bill(orders, target):
    """Split `target` across `orders` in proportion to their cost in cedis.

    A more expensive item carries a bigger share of the bill.
    Returns ({order.pk: share}, [orders with no cost in cedis]).
    The shares always add up to exactly `target`.
    """
    weights = {}
    missing = []
    for order in orders:
        weight = order_weight(order)
        if weight:
            weights[order.pk] = weight
        else:
            missing.append(order)

    if not weights or target is None:
        return {}, missing

    total = sum(weights.values())
    shares = {
        pk: (target * weight / total).quantize(CENT, rounding=ROUND_HALF_UP)
        for pk, weight in weights.items()
    }
    difference = target - sum(shares.values())
    if difference:
        shares[max(weights, key=weights.get)] += difference
    return shares, missing