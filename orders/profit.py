from decimal import Decimal

ZERO = Decimal("0")


def batch_profit(batch, orders):
    """Profit for one batch, in cedis.

    Only orders with both a selling price and a cost in cedis are counted, so
    the sales and costs compared are always for the same orders.
    """
    counted = [
        o for o in orders if o.quoted_price and o.cost_in_grid_currency is not None
    ]
    sales = sum((o.quoted_price for o in counted), ZERO)
    cost = sum((o.cost_in_grid_currency for o in counted), ZERO)
    item_profit = sales - cost

    fees_set = sum((o.shipping_fee for o in orders if o.shipping_fee is not None), ZERO)
    bill = batch.freight_total
    shipping_result = (fees_set - bill) if bill else None

    return {
        "sales": sales,
        "cost": cost,
        "item_profit": item_profit,
        "fees_set": fees_set,
        "bill": bill,
        "shipping_result": shipping_result,
        "total": item_profit + (shipping_result or ZERO),
        "skipped": len(orders) - len(counted),
    }